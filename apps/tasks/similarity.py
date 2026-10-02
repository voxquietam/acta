"""Semantic neighbours: "something that looks like this already exists".

Acta's tasks are written in Ukrainian, Russian and English at once, often
inside one project, and people search in whichever language they think
in. Substring search cannot cross that gap — ``ILIKE`` will never match
"аудит сегментации сети" against *Network segmentation audit* — so the
text is turned into a vector by a multilingual embedding model and
compared by angle instead of by letters.

The model runs on an Ollama host on the network (``ACTA_EMBEDDING_URL``);
nothing is loaded into this process. Measured on the production database
(945 tasks, ``bge-m3``): 14 ms to embed one task, 26 ms for a query,
3.9 MB for every vector in the workspace. Of the links people had made
themselves, 70% of ``related`` pairs came back inside the top ten.

Three rules this module keeps:

- **It is optional.** With no URL configured — or with the host down —
  every entry point returns nothing. Creating a task must never fail
  because a side feature could not reach a box on the network.
- **Only like-for-like is compared.** Rows carry the model that built
  them; after a model change the old vectors are invisible rather than
  wrong, until the backfill command rebuilds them.
- **Recurring instances are not neighbours.** A weekly task has dozens of
  identical copies by design; offering them as duplicates would make the
  warning worthless. Production had five "Експорт працівників з ІАС" at
  cosine 1.000, all of them generated.

See ``docs/decisions/0034-task-similarity.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Iterable, Sequence
import urllib.error
import urllib.request

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max

logger = logging.getLogger(__name__)

#: Longest slice of a description that travels with the title. The model
#: window is far wider (8k tokens), but a long text averages out into a
#: vector that matches nothing in particular; production's 90th percentile
#: description is 379 characters, so this cuts almost nothing.
MAX_TEXT = 2000

#: Texts per HTTP call during a backfill. Keeps one request well under the
#: batch timeout on a host that may be busy with other models.
BATCH = 32

#: Below this cosine two tasks are not worth showing each other. Deliberately
#: low: the caller asks for a handful and the ranking does the work, while a
#: strict threshold would depend on the model's own scale.
#:
#: Both numbers are provisional and want calibrating against real
#: judgements — on bge-m3 a true match has measured anywhere from 0.55 to
#: 0.88, which is why nothing here treats the score as a verdict.
MIN_SCORE = 0.45

#: The create dialog is stricter than the MCP tool: a client reads the
#: titles and discards a bad suggestion for free, whereas a box of three
#: weak guesses under the title field is noise on every keystroke.
HINT_MIN_SCORE = 0.55

#: Suggesting a person is the strictest of the three. Being told the
#: wrong colleague usually does this is worse than being told nothing,
#: so the neighbours have to be close and the person has to recur.
ASSIGNEE_MIN_SCORE = 0.62
ASSIGNEE_MIN_HITS = 2


class EmbeddingUnavailable(RuntimeError):
    """The embedding host could not be reached or refused the request."""


def is_enabled() -> bool:
    """Return whether an embedding host is configured at all."""
    return bool(getattr(settings, "ACTA_EMBEDDING_URL", ""))


def text_for(task) -> str:
    """Return the text that represents ``task`` to the model."""
    description = (task.description or "").strip()
    return f"{task.title}\n{description}".strip()[:MAX_TEXT]


def digest(text: str) -> str:
    """Return the hash recorded alongside a vector to detect edits."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed(texts: Sequence[str], *, timeout: float | None = None) -> list[list[float]]:
    """Embed every text through the configured host, normalised.

    Args:
        texts: Raw strings to embed.
        timeout: Seconds to wait; defaults to the read timeout.

    Returns:
        One unit-length vector per input, in the same order.

    Raises:
        EmbeddingUnavailable: No host configured, or it could not be
            reached, or it answered something that is not a vector.
    """
    if not is_enabled():
        raise EmbeddingUnavailable("no embedding host configured")
    if not texts:
        return []

    import numpy as np

    host = settings.ACTA_EMBEDDING_URL.rstrip("/")
    wait = settings.ACTA_EMBEDDING_TIMEOUT if timeout is None else timeout
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        end = start + BATCH
        payload = json.dumps(
            {"model": settings.ACTA_EMBEDDING_MODEL, "input": list(texts[start:end])},
        ).encode()
        request = urllib.request.Request(
            f"{host}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=wait) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingUnavailable(f"{host}: {exc}") from exc
        raw = body.get("embeddings")
        if not raw or len(raw) != len(texts[start:end]):
            raise EmbeddingUnavailable(f"{host}: answered {len(raw or [])} vectors for {end - start} texts")
        matrix = np.asarray(raw, dtype=np.float32)
        matrix /= np.clip(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9, None)
        vectors.extend(matrix.tolist())
    return vectors


def store(tasks: Iterable, *, force: bool = False) -> int:
    """Rebuild the stored vectors for ``tasks``; return how many changed.

    Tasks whose text has not changed since their vector was built are
    skipped, so this is cheap to call on a whole project.

    Args:
        tasks: Task instances with ``project__workspace_id`` available.
        force: Rebuild even when the hash and model still match.

    Returns:
        The number of vectors written.

    Raises:
        EmbeddingUnavailable: The host could not be reached.
    """
    import numpy as np

    from apps.tasks.models import TaskEmbedding

    tasks = [task for task in tasks if task.archived_at is None]
    if not tasks:
        return 0
    model = settings.ACTA_EMBEDDING_MODEL
    existing = {row.task_id: row for row in TaskEmbedding.objects.filter(task_id__in=[task.pk for task in tasks])}

    pending = []
    for task in tasks:
        text = text_for(task)
        if not text:
            continue
        row = existing.get(task.pk)
        if not force and row and row.model == model and row.text_hash == digest(text):
            continue
        pending.append((task, text))
    if not pending:
        return 0

    vectors = embed([text for _, text in pending], timeout=settings.ACTA_EMBEDDING_BATCH_TIMEOUT)
    written = 0
    touched = set()
    for (task, text), vector in zip(pending, vectors):
        packed = np.asarray(vector, dtype=np.float32).tobytes()
        TaskEmbedding.objects.update_or_create(
            task=task,
            defaults={
                "workspace_id": task.project.workspace_id,
                "vector": packed,
                "dimensions": len(vector),
                "model": model,
                "text_hash": digest(text),
            },
        )
        written += 1
        touched.add(task.project.workspace_id)
    for workspace_id in touched:
        _CACHE.pop(workspace_id, None)
    return written


def schedule(task) -> None:
    """Queue a rebuild of one task's vector, after the transaction commits.

    The embedding host is a network hop and the caller is usually a web
    request, so the work goes to django-q exactly the way Telegram
    delivery does. Nothing is queued when the feature is off.
    """
    if not is_enabled():
        return

    def _enqueue():
        from django_q.tasks import async_task

        async_task("apps.tasks.similarity.rebuild_one", task.pk)

    transaction.on_commit(_enqueue)


def rebuild_one(task_id: int) -> None:
    """django-q entry point — rebuild one task's vector by id.

    Top-level so django-q can resolve the dotted path, and best-effort:
    a task deleted between enqueue and pickup, or an unreachable host,
    must not turn into a failed job that someone has to look at.
    """
    from apps.tasks.models import Task

    task = Task.objects.select_related("project").filter(pk=task_id).first()
    if task is None:
        return
    try:
        store([task])
    except EmbeddingUnavailable as exc:
        logger.warning("embedding unavailable for task %s: %s", task_id, exc)


# ---- reading -------------------------------------------------------------

#: One matrix per workspace, kept between requests. Rebuilt when the row
#: count or the newest ``updated_at`` moves, which one cheap aggregate
#: answers — far cheaper than shipping 4 MB of vectors on every keystroke
#: in the create dialog.
_CACHE: dict[int, tuple[tuple, list[int], object]] = {}


def _stamp(workspace_id: int) -> tuple:
    """Return a cheap fingerprint of a workspace's stored vectors."""
    from apps.tasks.models import TaskEmbedding

    row = TaskEmbedding.objects.filter(
        workspace_id=workspace_id,
        model=settings.ACTA_EMBEDDING_MODEL,
    ).aggregate(n=Count("id"), last=Max("updated_at"))
    return (row["n"], row["last"])


def _matrix(workspace_id: int):
    """Return ``(task_ids, matrix)`` for a workspace, from cache when fresh."""
    import numpy as np

    from apps.tasks.models import TaskEmbedding

    stamp = _stamp(workspace_id)
    cached = _CACHE.get(workspace_id)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]

    rows = list(
        TaskEmbedding.objects.filter(
            workspace_id=workspace_id,
            model=settings.ACTA_EMBEDDING_MODEL,
            task__archived_at__isnull=True,
            # A recurring rule stamps out identical copies by design; they
            # are not duplicates of each other and must not be offered.
            task__recurrence__isnull=True,
        ).values_list("task_id", "vector", "dimensions")
    )
    if not rows:
        empty = np.zeros((0, 0), dtype=np.float32)
        _CACHE[workspace_id] = (stamp, [], empty)
        return [], empty

    width = rows[0][2]
    ids = [task_id for task_id, _, dims in rows if dims == width]
    matrix = np.vstack(
        [np.frombuffer(vector, dtype="<f4") for _, vector, dims in rows if dims == width],
    )
    _CACHE[workspace_id] = (stamp, ids, matrix)
    return ids, matrix


def neighbours_of_text(
    text: str,
    *,
    workspace_id: int,
    limit: int = 5,
    exclude_ids: Sequence[int] = (),
    min_score: float = MIN_SCORE,
) -> list[tuple[int, float]]:
    """Return ``(task_id, score)`` for the tasks closest to ``text``.

    Returns an empty list when the feature is off, the host is
    unreachable, or the workspace has no vectors yet — every caller
    treats "no neighbours" and "no answer" the same way, because there is
    nothing useful to tell a user about an embedding host.
    """
    import numpy as np

    text = (text or "").strip()
    if not text or not is_enabled():
        return []
    try:
        query = embed([text[:MAX_TEXT]])[0]
    except EmbeddingUnavailable as exc:
        logger.info("similarity lookup skipped: %s", exc)
        return []

    return _rank(
        np.asarray(query, dtype=np.float32),
        workspace_id=workspace_id,
        limit=limit,
        exclude_ids=exclude_ids,
        min_score=min_score,
    )


def _rank(query, *, workspace_id: int, limit: int, exclude_ids: Sequence[int], min_score: float):
    """Score one vector against a workspace's matrix and take the top of it."""
    import numpy as np

    ids, matrix = _matrix(workspace_id)
    if not ids or matrix.shape[1] != len(query):
        return []

    scores = matrix @ query
    skip = set(exclude_ids)
    order = np.argsort(-scores)[: limit + len(skip)]
    found = []
    for position in order:
        task_id = ids[int(position)]
        score = float(scores[int(position)])
        if task_id in skip or score < min_score:
            continue
        found.append((task_id, score))
        if len(found) >= limit:
            break
    return found


def labels_of(
    found: Sequence[tuple[int, float]],
    *,
    limit: int = 3,
    skip_label_ids: Sequence[int] = (),
) -> list[tuple[int, int]]:
    """Return ``(label_id, how many)`` for the labels the neighbours carry.

    Same shape and the same caution as :func:`assignees_of`: only the
    close end of the list counts, and a label has to appear on at least
    two neighbours before it is worth putting in front of someone. Labels
    the task already carries are passed in as ``skip_label_ids`` rather
    than filtered afterwards, so the limit is spent on useful rows.
    """
    from apps.tasks.models import Task

    close = [task_id for task_id, score in found if score >= ASSIGNEE_MIN_SCORE]
    if not close:
        return []
    skip = set(skip_label_ids)
    counts: dict[int, int] = {}
    rows = Task.labels.through.objects.filter(task_id__in=close).values_list("label_id", flat=True)
    for label_id in rows:
        if label_id in skip:
            continue
        counts[label_id] = counts.get(label_id, 0) + 1
    ranked = sorted(
        ((label_id, hits) for label_id, hits in counts.items() if hits >= ASSIGNEE_MIN_HITS),
        key=lambda pair: -pair[1],
    )
    return ranked[:limit]


def stored_vector(task):
    """Return ``task``'s own vector if it is current, else ``None``.

    An existing task has already been through the model, so asking for
    its neighbours should cost no round trip at all — which matters,
    because the task page asks on every load.
    """
    import numpy as np

    row = getattr(task, "embedding", None)
    if row is None or row.model != settings.ACTA_EMBEDDING_MODEL:
        return None
    if row.text_hash != digest(text_for(task)):
        return None
    return np.frombuffer(row.vector, dtype="<f4")


def neighbours_of_task(task, *, limit: int = 5, min_score: float = MIN_SCORE) -> list[tuple[int, float]]:
    """Return the tasks closest to ``task``, itself excluded."""
    vector = stored_vector(task)
    if vector is not None:
        return _rank(
            vector,
            workspace_id=task.project.workspace_id,
            limit=limit,
            exclude_ids=[task.pk],
            min_score=min_score,
        )
    return neighbours_of_text(
        text_for(task),
        workspace_id=task.project.workspace_id,
        limit=limit,
        exclude_ids=[task.pk],
        min_score=min_score,
    )


def likely_assignees(task, *, limit: int = 2, pool: int = 12) -> list[tuple[int, int]]:
    """Return ``(user_id, how many)`` for the people who do work like this.

    Derived from who the near neighbours were given to. One shared
    assignee on one similar task is a coincidence, so a person has to
    appear at least twice before they are worth suggesting, and the
    neighbours themselves are taken at a stricter cut than the board
    uses — a wrong name next to someone's work is more annoying than no
    name at all.

    Args:
        task: The task being assigned.
        limit: How many people to return, most frequent first.
        pool: How many neighbours to count over.

    Returns:
        ``(user_id, count)`` pairs, or an empty list when nothing is
        frequent enough to mean anything.
    """
    found = neighbours_of_task(task, limit=pool, min_score=ASSIGNEE_MIN_SCORE)
    return assignees_of(found, limit=limit, skip_user_id=task.assignee_id)


def assignees_of(
    found: Sequence[tuple[int, float]],
    *,
    limit: int = 2,
    skip_user_id: int | None = None,
) -> list[tuple[int, int]]:
    """Count who owns the tasks in ``found`` and return the recurring names.

    Split out from :func:`likely_assignees` because the create dialog has
    no task yet: it already holds the neighbours of what is being typed
    and must not pay for a second lookup to ask who usually takes them.
    Pairs below :data:`ASSIGNEE_MIN_SCORE` are dropped here rather than
    by the caller, so every entry point applies the same stricter cut.
    """
    from apps.tasks.models import Task

    close = [task_id for task_id, score in found if score >= ASSIGNEE_MIN_SCORE]
    if not close:
        return []
    counts: dict[int, int] = {}
    rows = Task.objects.filter(pk__in=close, assignee__isnull=False).values_list("assignee_id", flat=True)
    for assignee_id in rows:
        counts[assignee_id] = counts.get(assignee_id, 0) + 1
    ranked = sorted(
        ((user_id, hits) for user_id, hits in counts.items() if hits >= ASSIGNEE_MIN_HITS and user_id != skip_user_id),
        key=lambda pair: -pair[1],
    )
    return ranked[:limit]


# ---- keeping the vectors current -----------------------------------------

#: Saving a task is not the same as changing what it says. A kanban drag
#: writes ``status`` and ``order`` twenty times a minute; re-queueing a
#: vector for each of those would fill the worker queue with jobs whose
#: only outcome is "the hash still matches".
TEXT_FIELDS = {"title", "description"}


def on_task_saved(sender, instance, created, update_fields=None, **kwargs):
    """Queue a rebuild when a task's text may have changed.

    Connected in :class:`apps.tasks.apps.TasksConfig`. A save that names
    its ``update_fields`` and touches neither the title nor the
    description is ignored; anything else goes to the worker, which skips
    the HTTP call itself if the text turns out to be unchanged.
    """
    if not is_enabled() or instance.archived_at is not None:
        return
    if not created and update_fields is not None and not (set(update_fields) & TEXT_FIELDS):
        return
    schedule(instance)
