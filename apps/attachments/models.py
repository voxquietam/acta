from pathlib import Path
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


def attachment_upload_to(instance: "Attachment", filename: str) -> str:
    """Build the storage path for an attachment's file.

    Layout: ``attachments/<workspace_id>/<owner_type>/<owner_id>/<uuid>.<ext>``.
    Scoping by workspace keeps the auth-gated membership check and any
    future per-workspace quota simple; the UUID name avoids collisions and
    leaks nothing about the original filename (kept in ``original_name``).

    Args:
        instance: The ``Attachment`` being saved. Its owner FK and
            ``workspace_id`` must already be set (they are, since the file
            is written during ``Model.save()`` after attribute assignment).
        filename: The client-supplied filename, used only for its extension.

    Returns:
        The storage-relative path the file is written to under MEDIA_ROOT.
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    owner_type, owner_id = instance.owner_ref
    name = uuid.uuid4().hex
    if ext:
        name = f"{name}.{ext}"
    return f"attachments/{instance.workspace_id}/{owner_type}/{owner_id}/{name}"


class Attachment(models.Model):
    """A file uploaded against a task, a comment, or a project description.

    Targets exactly one of ``task`` / ``comment`` / ``project`` (enforced
    by a DB check constraint), mirroring the polymorphic-ownership pattern
    of :class:`apps.comments.models.Comment` and
    :class:`apps.reactions.models.Reaction` — explicit nullable FKs, no
    content types. ``project`` carries inline editor images embedded in a
    project description; ``task`` carries both panel attachments and inline
    images in a task description, told apart by ``kind``. See
    docs/decisions/0025-file-storage.md.

    Files are never served publicly — only through the auth-gated download
    view after a workspace-membership check.
    """

    KIND_FILE = "file"
    KIND_INLINE_IMAGE = "inline_image"
    KIND_CHOICES = [
        (KIND_FILE, _("File attachment")),
        (KIND_INLINE_IMAGE, _("Inline editor image")),
    ]

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="attachments",
        help_text="Workspace this file belongs to. Denormalized for access-control filtering and path scoping",
    )
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        help_text="Task this file is attached to. Null when it targets a comment or a project",
    )
    comment = models.ForeignKey(
        "comments.Comment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        help_text="Comment this file is attached to. Null when it targets a task or a project",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        help_text="Project whose description embeds this inline image. Null when it targets a task or a comment",
    )
    kind = models.CharField(
        max_length=20,
        choices=KIND_CHOICES,
        default=KIND_FILE,
        help_text="Whether this is a panel file attachment or an image embedded in a description editor",
    )
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_attachments",
        help_text="User who uploaded the file. Null after that user is deleted",
    )
    file = models.FileField(
        upload_to=attachment_upload_to,
        max_length=255,
        help_text="The stored file, written under MEDIA_ROOT and served only through the auth-gated download view",
    )
    original_name = models.CharField(
        max_length=255,
        help_text="Original client-supplied filename, shown in the UI and used for the download name",
    )
    content_type = models.CharField(
        max_length=120,
        help_text="Sniffed MIME type of the stored file, set on upload and trusted thereafter",
    )
    size = models.PositiveBigIntegerField(
        help_text="Stored file size in bytes, after image normalization when applicable",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the file was uploaded",
    )

    class Meta:
        verbose_name = _("Attachment")
        verbose_name_plural = _("Attachments")
        ordering = [
            "created_at",
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(task__isnull=False, comment__isnull=True, project__isnull=True)
                    | models.Q(task__isnull=True, comment__isnull=False, project__isnull=True)
                    | models.Q(task__isnull=True, comment__isnull=True, project__isnull=False)
                ),
                name="attachment_exactly_one_owner",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "workspace",
                    "kind",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the original filename and the owner it hangs off."""
        return f"{self.original_name} on {self.task or self.comment or self.project}"

    @property
    def owner_ref(self) -> tuple[str, int]:
        """Return ``(owner_type, owner_id)`` for path building.

        Returns:
            The owner-type token (``task`` / ``comment`` / ``project``) and
            the owner's id. Falls back to ``("orphan", 0)`` before any
            owner is assigned (e.g. mid-construction).
        """
        if self.task_id:
            return ("task", self.task_id)
        if self.comment_id:
            return ("comment", self.comment_id)
        if self.project_id:
            return ("project", self.project_id)
        return ("orphan", 0)

    @property
    def is_image(self) -> bool:
        """Return True when the stored content type is an image."""
        return self.content_type.startswith("image/")

    def clean(self) -> None:
        """Validate that exactly one owner FK is set.

        Raises:
            ValidationError: If zero or more than one of task / comment /
                project is set.
        """
        owners = [self.task_id, self.comment_id, self.project_id]
        if sum(1 for owner in owners if owner) != 1:
            raise ValidationError(_("An attachment must target exactly one of a task, a comment, or a project."))
