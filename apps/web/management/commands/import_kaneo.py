"""Import Kaneo data (users, projects, tasks, comments, labels) into Acta.

The command consumes per-table JSON dumps produced by ``pg_dump`` against
the Kaneo Postgres instance (see ``kaneo_dump/README.md`` in the repo for
the exact dump commands). It runs the whole import inside a single
transaction and rolls back unless ``--apply`` is passed.

Mapping summary (Kaneo → Acta):

* ``workspace``                  → reused/created Acta workspace (one ws)
* ``project``                    → ``projects.Project`` (slug_prefix derived
                                   from Kaneo ``project.slug``)
* ``task``                       → ``tasks.Task`` (numbers preserved via
                                   ``bulk_create``; ``next_task_number`` set
                                   to ``max(number)+1`` per project)
* ``task.status='archived'``     → ``status=done`` + ``archived_at=updated_at``
* ``task_relation`` relation_type=
    - ``related``                → ``Task.related`` (symmetric M2M)
    - ``subtask``                → ignored (per migration decision)
* ``comment``                    → ``comments.Comment``
* ``label`` (both workspace
  templates and per-task copies) → ``labels.Label`` collapsed by ``name``
                                   inside the target workspace; attached
                                   via ``Task.labels`` for per-task rows
* ``user``                       → existing Acta user by email **or** a
                                   placeholder ``User(is_active=False)`` with
                                   an unusable password, also added to the
                                   target workspace as a regular member

Activity log entries are intentionally **not** written for this bulk
migration; ``log_event()`` is view-layer only by design.

Usage::

    docker compose exec web python manage.py import_kaneo \
        --dump-dir kaneo_dump \
        --target-workspace-slug ksu24 \
        --target-workspace-name KSU24 \
        --owner-email admin@gmail.com \
        [--apply]

Without ``--apply`` the command runs as a dry-run and rolls back at the
end. The output prints counts per stage so you can diff dry-run vs apply.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as djtz

from apps.accounts.models import User
from apps.comments.models import Comment
from apps.labels.models import Label
from apps.projects.models import Project
from apps.tasks.models import Task
from apps.workspaces.models import Workspace, WorkspaceMember

# --- mappings ---------------------------------------------------------------

# Kaneo task.status → Acta Task.STATUS_*. Kaneo `archived` is a separate
# status string in their model; in Acta it is an orthogonal `archived_at`
# timestamp on top of an underlying status (we collapse it onto `done`).
STATUS_MAP: dict[str, str] = {
    "to-do": Task.STATUS_TODO,
    "in-progress": Task.STATUS_IN_PROGRESS,
    "in-review": Task.STATUS_IN_REVIEW,
    "done": Task.STATUS_DONE,
    "planned": Task.STATUS_PLANNED,
    "archived": Task.STATUS_DONE,
}

# Kaneo task.priority (text) → Acta Task priority (SmallInteger 0..4).
PRIORITY_MAP: dict[str, int] = {
    "urgent": Task.URGENT,
    "high": Task.HIGH,
    "medium": Task.MEDIUM,
    "low": Task.LOW,
    "no-priority": Task.NO_PRIORITY,
}

# Kaneo named colors → hex equivalents close to Acta's design tokens.
# Anything unknown falls back to ``DEFAULT_LABEL_COLOR``.
COLOR_MAP: dict[str, str] = {
    "red": "#f43f5e",
    "orange": "#f97316",
    "yellow": "#f59e0b",
    "green": "#10b981",
    "teal": "#14b8a6",
    "blue": "#3b82f6",
    "indigo": "#6366f1",
    "purple": "#8b5cf6",
    "pink": "#ec4899",
    "gray": "#71717a",
    "dark-gray": "#52525b",
    "brown": "#92400e",
}
DEFAULT_LABEL_COLOR = "#71717a"


# --- helpers ----------------------------------------------------------------


def parse_dt(value: str | None) -> datetime | None:
    """Parse a Kaneo timestamp string into an aware UTC datetime."""
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_date(value: str | None) -> date | None:
    """Parse a Kaneo timestamp into a naive date (used for Task date fields)."""
    dt = parse_dt(value)
    return dt.date() if dt else None


def derive_slug_prefix(kaneo_slug: str, kaneo_name: str) -> str:
    """Return an ``[A-Z]{2,6}`` slug prefix derived from Kaneo metadata."""
    letters = "".join(ch for ch in (kaneo_slug or "").upper() if "A" <= ch <= "Z")
    if 2 <= len(letters) <= 6:
        return letters
    name_letters = "".join(ch for ch in (kaneo_name or "").upper() if "A" <= ch <= "Z")
    return (name_letters[:6] or "PROJ")[:6]


def normalize_color(kaneo_color: str | None) -> str:
    """Translate a Kaneo label color (name or hex) into a hex string."""
    if not kaneo_color:
        return DEFAULT_LABEL_COLOR
    value = kaneo_color.strip()
    if value.startswith("#"):
        return value
    return COLOR_MAP.get(value.lower(), DEFAULT_LABEL_COLOR)


def split_name(full: str | None) -> tuple[str, str]:
    """Split a Kaneo user's full name into Django ``first_name``/``last_name``."""
    if not full:
        return "", ""
    parts = full.strip().split()
    if not parts:
        return "", ""
    first = parts[0][:150]
    last = " ".join(parts[1:])[:150]
    return first, last


# --- command ----------------------------------------------------------------


class Command(BaseCommand):
    """One-shot Kaneo → Acta importer (see module docstring for the mapping)."""

    help = "Import Kaneo data dumps (JSON) into a target Acta workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dump-dir",
            default="kaneo_dump",
            help="Directory containing Kaneo JSON dumps (default: kaneo_dump)",
        )
        parser.add_argument(
            "--target-workspace-slug",
            required=True,
            help="Acta workspace slug to create/use as the import target",
        )
        parser.add_argument(
            "--target-workspace-name",
            default=None,
            help="Display name when the target workspace is newly created (defaults to slug)",
        )
        parser.add_argument(
            "--owner-email",
            required=True,
            help="Email of the existing Acta user who will own the workspace if newly created",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Commit changes. Without this flag the transaction is rolled back at the end.",
        )

    def handle(self, *args, **opts):
        dump_dir = Path(opts["dump_dir"])
        if not dump_dir.is_dir():
            raise CommandError(f"Dump directory not found: {dump_dir}")
        self.dump_dir = dump_dir
        self.apply = bool(opts["apply"])

        ws_slug = opts["target_workspace_slug"].strip()
        ws_name = (opts.get("target_workspace_name") or ws_slug).strip()
        owner_email = opts["owner_email"].strip().lower()

        try:
            owner = User.objects.get(email__iexact=owner_email)
        except User.DoesNotExist:
            raise CommandError(f"Owner email not found in Acta: {owner_email}")

        kaneo = self._load_dumps()
        self.stdout.write(
            self.style.NOTICE(
                f"[{'APPLY' if self.apply else 'DRY-RUN'}] dump={dump_dir} " f"target_ws={ws_slug} owner={owner.email}"
            )
        )
        self.stdout.write(
            f"  loaded: {len(kaneo['workspace'])} ws, {len(kaneo['project'])} projects, "
            f"{len(kaneo['task'])} tasks, {len(kaneo['comment'])} comments, "
            f"{len(kaneo['label'])} labels, {len(kaneo['user'])} users, "
            f"{len(kaneo['task_relation'])} relations"
        )

        with transaction.atomic():
            sid = transaction.savepoint()
            try:
                self._run(kaneo, ws_slug, ws_name, owner)
            except Exception:
                transaction.savepoint_rollback(sid)
                raise
            if self.apply:
                transaction.savepoint_commit(sid)
                self.stdout.write(self.style.SUCCESS("APPLY: committed"))
            else:
                transaction.savepoint_rollback(sid)
                self.stdout.write(self.style.WARNING("DRY-RUN: rolled back"))

    # --- pipeline ----------------------------------------------------------

    def _load_dumps(self) -> dict[str, list]:
        names = ("workspace", "project", "task", "task_relation", "comment", "label", "user")
        data: dict[str, list] = {}
        for name in names:
            path = self.dump_dir / f"{name}.json"
            if not path.exists():
                raise CommandError(f"Missing dump file: {path}")
            data[name] = json.loads(path.read_text())
        return data

    def _run(self, kaneo: dict[str, list], ws_slug: str, ws_name: str, owner: User) -> None:
        workspace = self._ensure_workspace(ws_slug, ws_name, owner)
        user_map = self._import_users(kaneo["user"], workspace)
        project_map = self._import_projects(kaneo["project"], workspace)
        task_map = self._import_tasks(kaneo["task"], project_map, user_map)
        self._import_relations(kaneo["task_relation"], task_map)
        self._import_labels(kaneo["label"], workspace, task_map)
        self._import_comments(kaneo["comment"], task_map, user_map)

    # --- stages ------------------------------------------------------------

    def _ensure_workspace(self, slug: str, name: str, owner: User) -> Workspace:
        workspace, created = Workspace.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "owner": owner},
        )
        if created:
            WorkspaceMember.objects.create(
                workspace=workspace,
                user=owner,
                role=WorkspaceMember.OWNER,
            )
            self.stdout.write(f"  workspace: created '{slug}' owned by {owner.email}")
            return workspace
        if Project.objects.filter(workspace=workspace).exists():
            raise CommandError(
                f"Workspace '{slug}' already exists and has projects. "
                f"Refusing to mix imported data with existing content."
            )
        self.stdout.write(f"  workspace: reusing empty '{slug}'")
        return workspace

    def _import_users(self, kaneo_users: list[dict], workspace: Workspace) -> dict[str, User]:
        user_map: dict[str, User] = {}
        matched = created = 0
        for ku in kaneo_users:
            email = (ku.get("email") or "").strip().lower()
            if not email:
                continue
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                first, last = split_name(ku.get("name"))
                username = self._unique_username(f"kaneo-{ku['id'][:8]}")
                user = User(
                    username=username,
                    email=email,
                    first_name=first,
                    last_name=last,
                    is_active=False,
                )
                user.set_unusable_password()
                user.save()
                created += 1
            else:
                matched += 1
            WorkspaceMember.objects.get_or_create(
                workspace=workspace,
                user=user,
                defaults={"role": WorkspaceMember.MEMBER},
            )
            user_map[ku["id"]] = user
        self.stdout.write(f"  users: {matched} matched by email, {created} placeholders created")
        return user_map

    def _unique_username(self, base: str) -> str:
        candidate = base
        suffix = 2
        while User.objects.filter(username=candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _import_projects(self, kaneo_projects: list[dict], workspace: Workspace) -> dict[str, Project]:
        project_map: dict[str, Project] = {}
        used_prefixes: set[str] = set(Project.objects.filter(workspace=workspace).values_list("slug_prefix", flat=True))
        for kp in kaneo_projects:
            prefix = derive_slug_prefix(kp["slug"], kp["name"])
            if prefix in used_prefixes:
                prefix = self._dedupe_prefix(prefix, used_prefixes)
            used_prefixes.add(prefix)
            project = Project.objects.create(
                workspace=workspace,
                name=(kp.get("name") or kp["slug"])[:120],
                slug_prefix=prefix,
                description=kp.get("description") or "",
                icon="",
                archived=bool(kp.get("archived_at")),
            )
            # `created_at` is auto_now_add; bulk_create would honour an
            # explicit value but Project uses .objects.create() here, so
            # patch it via an UPDATE to preserve Kaneo timestamps.
            created_at = parse_dt(kp.get("created_at"))
            if created_at:
                Project.objects.filter(pk=project.pk).update(created_at=created_at)
            project_map[kp["id"]] = project
        self.stdout.write(f"  projects: {len(project_map)} created")
        return project_map

    def _dedupe_prefix(self, base: str, used: set[str]) -> str:
        for i in range(2, 100):
            candidate = (base + str(i))[:6]
            if candidate not in used:
                return candidate
        raise CommandError(f"Cannot find a unique slug_prefix derived from '{base}'")

    def _import_tasks(
        self,
        kaneo_tasks: list[dict],
        project_map: dict[str, Project],
        user_map: dict[str, User],
    ) -> dict[str, Task]:
        by_project: dict[str, list[dict]] = defaultdict(list)
        for kt in kaneo_tasks:
            by_project[kt["project_id"]].append(kt)

        instances: list[Task] = []
        kaneo_id_by_index: list[str] = []
        archived_ids: set[str] = set()

        for kpid, ktasks in by_project.items():
            project = project_map.get(kpid)
            if project is None:
                self.stdout.write(self.style.WARNING(f"  skipping {len(ktasks)} tasks from missing project {kpid}"))
                continue
            ktasks.sort(key=lambda t: t["number"])
            max_num = 0
            for kt in ktasks:
                kaneo_status = kt.get("status") or "planned"
                acta_status = STATUS_MAP.get(kaneo_status, Task.STATUS_PLANNED)
                acta_priority = PRIORITY_MAP.get(kt.get("priority") or "no-priority", Task.NO_PRIORITY)
                created_at = parse_dt(kt.get("created_at")) or djtz.now()
                updated_at = parse_dt(kt.get("updated_at")) or created_at
                end_date = None
                completed_at = None
                if acta_status == Task.STATUS_DONE:
                    # Mirror Task.save()._sync_done_dates() for bulk inserts.
                    completed_at = updated_at
                    end_date = updated_at.date()
                instance = Task(
                    project=project,
                    number=kt["number"],
                    title=(kt.get("title") or "")[:200],
                    description=kt.get("description") or "",
                    status=acta_status,
                    priority=acta_priority,
                    due_date=parse_date(kt.get("due_date")),
                    start_date=parse_date(kt.get("start_date")),
                    end_date=end_date,
                    assignee=user_map.get(kt.get("assignee_id")),
                    created_at=created_at,
                    updated_at=updated_at,
                    completed_at=completed_at,
                )
                if kaneo_status == "archived":
                    archived_ids.add(kt["id"])
                    instance.archived_at = updated_at
                instances.append(instance)
                kaneo_id_by_index.append(kt["id"])
                max_num = max(max_num, kt["number"])
            project.next_task_number = max_num + 1
            project.save(update_fields=["next_task_number"])

        Task.objects.bulk_create(instances)
        # bulk_create returns the same list with primary keys populated on
        # PostgreSQL — build the kaneo_id → Task map from that.
        task_map: dict[str, Task] = {kid: inst for kid, inst in zip(kaneo_id_by_index, instances)}
        self.stdout.write(f"  tasks: {len(instances)} created; archived: {len(archived_ids)}")
        return task_map

    def _import_relations(self, relations: list[dict], task_map: dict[str, Task]) -> None:
        added = skipped = 0
        for r in relations:
            if r.get("relation_type") != "related":
                skipped += 1
                continue
            src = task_map.get(r.get("source_task_id"))
            tgt = task_map.get(r.get("target_task_id"))
            if not (src and tgt):
                skipped += 1
                continue
            src.related.add(tgt)
            added += 1
        self.stdout.write(f"  relations: {added} 'related' edges added; {skipped} skipped")

    def _import_labels(
        self,
        kaneo_labels: list[dict],
        workspace: Workspace,
        task_map: dict[str, Task],
    ) -> None:
        # Pick canonical color per name: workspace template (task_id IS NULL)
        # wins; otherwise first non-empty value encountered.
        canonical_color: dict[str, str] = {}
        for kl in kaneo_labels:
            name = (kl.get("name") or "").strip()
            if not name:
                continue
            raw_color = kl.get("color") or ""
            if kl.get("task_id") is None:
                canonical_color[name] = raw_color
            elif name not in canonical_color:
                canonical_color[name] = raw_color
        # Create Acta labels (group=NULL — grouping is left to manual UI work).
        name_to_label: dict[str, Label] = {}
        for name, raw_color in canonical_color.items():
            label, _ = Label.objects.get_or_create(
                workspace=workspace,
                name=name[:60],
                defaults={"color": normalize_color(raw_color)},
            )
            name_to_label[name] = label
        attached = 0
        for kl in kaneo_labels:
            if not kl.get("task_id"):
                continue
            name = (kl.get("name") or "").strip()
            label = name_to_label.get(name)
            task = task_map.get(kl["task_id"])
            if label and task:
                task.labels.add(label)
                attached += 1
        self.stdout.write(f"  labels: {len(name_to_label)} unique created; {attached} attached to tasks")

    def _import_comments(
        self,
        kaneo_comments: list[dict],
        task_map: dict[str, Task],
        user_map: dict[str, User],
    ) -> None:
        created = skipped = 0
        for kc in kaneo_comments:
            task = task_map.get(kc.get("task_id"))
            if task is None:
                skipped += 1
                continue
            author = user_map.get(kc.get("user_id"))
            comment = Comment.objects.create(
                task=task,
                author=author,
                body=kc.get("content") or "",
            )
            created_at = parse_dt(kc.get("created_at"))
            updated_at = parse_dt(kc.get("updated_at")) or created_at
            if created_at:
                Comment.objects.filter(pk=comment.pk).update(
                    created_at=created_at,
                    updated_at=updated_at,
                )
            created += 1
        self.stdout.write(f"  comments: {created} created; {skipped} skipped")
