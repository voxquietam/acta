import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


def avatar_upload_to(instance: "User", filename: str) -> str:
    """Storage path for a user's avatar.

    Layout ``avatars/<user_id>/<uuid>.jpg``. Avatars are always
    normalized to JPEG on upload, so the extension is fixed; the UUID
    name busts the browser cache when a user replaces their photo.

    Args:
        instance: The ``User`` whose avatar is being saved (pk is set —
            avatars are only set on existing users).
        filename: Ignored except by convention; the stored name is a UUID.

    Returns:
        The storage-relative path under MEDIA_ROOT.
    """
    return f"avatars/{instance.pk}/{uuid.uuid4().hex}.jpg"


class User(AbstractUser):
    """Custom user model.

    Extension of :class:`AbstractUser` declared from day one so future
    fields (theme preference, display-name overrides) can be added
    without a destructive migration. Currently carries a per-user UI
    language preference; see docs/decisions/0018-i18n.md.
    """

    language = models.CharField(
        max_length=8,
        blank=True,
        choices=settings.LANGUAGES,
        help_text="Preferred UI language. Overrides browser cookie and Accept-Language",
    )
    favourite_projects = models.ManyToManyField(
        "projects.Project",
        blank=True,
        related_name="favourited_by",
        help_text=(
            "Projects this user has starred for quick access. The sidebar nav lists "
            "only these (with an empty-state CTA when the set is empty); the project "
            "list page renders a toggle star on every card to maintain the set"
        ),
    )
    active_workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=(
            "Workspace the user is currently scoped into. The sidebar switcher sets "
            "it; All Tasks / Projects / My Work / Inbox / My Activity are filtered to "
            "it. Null falls back to the user's first workspace by name"
        ),
    )
    avatar = models.ImageField(
        upload_to=avatar_upload_to,
        null=True,
        blank=True,
        max_length=255,
        help_text=(
            "Profile photo, square-cropped and resized to JPEG on upload; "
            "falls back to a colour-initial circle when unset"
        ),
    )

    class Meta(AbstractUser.Meta):
        verbose_name = _("User")
        verbose_name_plural = _("Users")

    @property
    def display_name(self) -> str:
        """Human-readable name shown across the UI.

        Returns ``First Last`` when either name field is populated,
        otherwise falls back to ``username``. Usernames stay reserved
        for ``@mention`` autocomplete and form-value identifiers.
        """
        full = self.get_full_name()
        return full or self.username

    @property
    def avatar_color(self) -> str:
        """Deterministic HSL colour for the no-photo avatar circle.

        Hashed from ``username`` (immutable identifier) so every page
        renders the same colour for a given user and renaming the
        display name doesn't shift the palette. Saturation / lightness
        are tuned so white initials stay readable on top.
        """
        digest = hashlib.md5(self.username.encode("utf-8")).hexdigest()
        hue = int(digest[:6], 16) % 360
        return f"hsl({hue}, 60%, 40%)"


class ApiToken(models.Model):
    """A revocable per-user API token for non-browser clients.

    Acta's web UI uses session auth. Programmatic clients (curl,
    scripts, the planned MCP server) need a stable credential that
    works without a browser session. Tokens are user-named so the
    creator can recognise which integration owns them
    (``"Claude Desktop"`` / ``"deploy script"`` / ``"INC-67 webhook"``)
    and revoke just the one if compromised.

    **Storage:** the plain-text secret is shown to the user ONCE at
    creation time, never persisted. The DB stores ``token_hash`` (a
    SHA-256 hex digest) and a short ``prefix`` (first 8 chars of the
    plain secret) so the user can identify tokens in the management
    UI without ever having to see the secret again. Lost-token
    recovery is intentionally not supported — revoke and create a new
    one.

    Lookup is done by hashing the incoming credential and matching
    ``token_hash`` directly; no per-row decryption pass, no leak of
    plain-text comparison.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_tokens",
        help_text="Owner of the token; the token authenticates as this user",
    )
    name = models.CharField(
        max_length=80,
        help_text="Human-readable label set by the user when minting the token",
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 hex digest of the plain secret; the secret itself is never stored",
    )
    prefix = models.CharField(
        max_length=8,
        help_text="First 8 chars of the plain secret, kept so the user can identify tokens in the UI",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the token was generated",
    )
    last_used_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last successful authentication with this token; null until first use",
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user (or an admin) revoked the token; revoked tokens fail authentication",
    )

    class Meta:
        verbose_name = _("API token")
        verbose_name_plural = _("API tokens")
        ordering = [
            "-created_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "token_hash",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the user-given name + prefix for admin / log readability."""
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def hash_secret(cls, secret: str) -> str:
        """Return the canonical SHA-256 hex digest used for lookup.

        Args:
            secret: Plain-text token as presented by the client.

        Returns:
            Lowercase 64-char hex digest. Matches ``token_hash`` storage.
        """
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @classmethod
    def generate(cls, *, user, name: str) -> tuple["ApiToken", str]:
        """Mint a new token and return ``(instance, plain_secret)``.

        The plain secret is shown to the user once at creation time
        and never persisted; only its hash is stored. Callers must
        surface ``plain_secret`` to the user in the response and warn
        that it cannot be retrieved later.

        Args:
            user: Owner of the new token.
            name: User-supplied label (e.g. ``"Claude Desktop"``).

        Returns:
            The persisted :class:`ApiToken` instance and the plain
            secret string the user copies into their client config.
        """
        plain = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name,
            token_hash=cls.hash_secret(plain),
            prefix=plain[:8],
        )
        return token, plain

    @property
    def is_active(self) -> bool:
        """True when the token can still authenticate (not revoked)."""
        return self.revoked_at is None
