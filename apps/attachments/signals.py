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

    Ref-counts the content-addressed dedup: the blob is kept while any
    other row still points at the same stored path. (The deleted row is
    already gone from the DB here, so this query sees only the survivors.)
    """
    if not instance.file:
        return
    name = instance.file.name
    if Attachment.objects.filter(file=name).exists():
        return
    instance.file.delete(save=False)
