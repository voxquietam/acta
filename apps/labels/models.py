from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$",
    message=_("Color must be a hex code: #RRGGBB or #RRGGBBAA " "(e.g. #a855f7 for purple)."),
)


class LabelGroup(models.Model):
    """Optional grouping of labels (Linear-style categories).

    A group can be marked ``is_exclusive`` to mean "at most one label from
    this group per task" — useful for mutually exclusive categories like
    ``Type: bug | feature | refactor``. See
    docs/decisions/0008-labels.md.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="label_groups",
        help_text="Workspace this group belongs to",
    )
    name = models.CharField(
        max_length=60,
        help_text='Group name shown in label pickers (e.g. "Type", "Priority", "Area")',
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Short guidance shown to the team — what kind of labels belong in this group. "
            "Surfaces in admin and the future label-management UI."
        ),
    )
    is_exclusive = models.BooleanField(
        default=False,
        help_text="If true, a task can have at most one label from this group",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the group was created",
    )

    class Meta:
        verbose_name = _("Label group")
        verbose_name_plural = _("Label groups")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workspace",
                    "name",
                ],
                name="labels_group_unique_workspace_name",
            ),
        ]

    def __str__(self) -> str:
        """Return the group name."""
        return self.name


class Label(models.Model):
    """A workspace-scoped tag attachable to tasks.

    Labels live at the workspace level (not per-project) and can optionally
    belong to a :class:`LabelGroup`.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="labels",
        help_text="Workspace this label belongs to",
    )
    group = models.ForeignKey(
        LabelGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labels",
        help_text="Optional group this label is part of",
    )
    name = models.CharField(
        max_length=60,
        help_text="Label display name",
    )
    color = models.CharField(
        max_length=9,
        validators=[HEX_COLOR_VALIDATOR],
        help_text="Hex color code (#RRGGBB or #RRGGBBAA). Required — labels must be visually distinguishable",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the label was created",
    )

    class Meta:
        verbose_name = _("Label")
        verbose_name_plural = _("Labels")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workspace",
                    "name",
                ],
                name="labels_unique_workspace_name",
            ),
        ]

    def __str__(self) -> str:
        """Return the label name."""
        return self.name
