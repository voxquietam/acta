from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Attachment


@receiver(post_delete, sender=Attachment)
def delete_attachment_file(sender, instance: Attachment, **kwargs) -> None:
    """Remove the stored file blob when its ``Attachment`` row is deleted.

    Django leaves ``FileField`` blobs on disk when a row is deleted, so a
    CASCADE from a deleted task / comment / project would otherwise orphan
    files on the tight VM disk. ``post_delete`` fires for cascaded and
    queryset deletions too, so this covers every path. ``save=False``
    avoids re-saving the row that no longer exists.
    """
    if instance.file:
        instance.file.delete(save=False)
