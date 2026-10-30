"""Database-backed queue: compare-and-swap claims work with SQLite and Postgres."""
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import Investigation, InvestigationEvent


def claim_next():
    candidate = Investigation.objects.filter(status="queued").order_by("created_at").values_list("pk", flat=True).first()
    if candidate is None:
        return None
    claimed = Investigation.objects.filter(pk=candidate, status="queued").update(
        status="running", attempt=F("attempt") + 1, heartbeat_at=timezone.now(),
        updated_at=timezone.now(), error="", finished_at=None,
    )
    return Investigation.objects.get(pk=candidate) if claimed else None


def recover_stale():
    cutoff = timezone.now() - timedelta(seconds=settings.BUGPILOT_LEASE_SECONDS)
    stale = Investigation.objects.filter(status="running", heartbeat_at__lt=cutoff)
    for run in stale:
        changed = Investigation.objects.filter(pk=run.pk, status="running", heartbeat_at__lt=cutoff).update(
            status="needs_attention", error="Worker stopped responding. Retry to recover this investigation safely.",
            finished_at=timezone.now(), updated_at=timezone.now(),
        )
        if changed:
            InvestigationEvent.objects.create(investigation=run, stage=run.stage, message="Worker lease expired. A retry will recover any existing pull request before continuing.")
