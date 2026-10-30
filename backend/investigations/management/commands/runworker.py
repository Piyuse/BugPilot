import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections
from django.utils import timezone

from investigations.models import Investigation, WorkerHeartbeat
from investigations.pipeline import run_investigation
from investigations.queue import claim_next, recover_stale


class Command(BaseCommand):
    help = "Process BugPilot's durable investigation queue. Run separately from the web server."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one investigation and exit.")
        parser.add_argument("--poll", type=float, default=2)

    def handle(self, *args, **options):
        self.stdout.write("BugPilot worker ready.")
        stop = threading.Event()
        try:
            while not stop.is_set():
                close_old_connections()
                WorkerHeartbeat.objects.update_or_create(pk=1, defaults={"updated_at": timezone.now()})
                recover_stale()
                run = claim_next()
                if run:
                    heartbeat_stop = threading.Event()

                    def heartbeat(run_id=run.pk, attempt=run.attempt, done=heartbeat_stop):
                        try:
                            while not done.wait(10):
                                close_old_connections()
                                Investigation.objects.filter(pk=run_id, status="running", attempt=attempt).update(heartbeat_at=timezone.now())
                                WorkerHeartbeat.objects.update_or_create(pk=1, defaults={"updated_at": timezone.now()})
                        finally:
                            connections.close_all()

                    thread = threading.Thread(target=heartbeat, daemon=True)
                    thread.start()
                    try:
                        result = run_investigation(run.pk)
                        self.stdout.write(f"{run.issue_key}: {result['status']}")
                    finally:
                        heartbeat_stop.set()
                        thread.join(timeout=12)
                if options["once"]:
                    break
                if not run:
                    stop.wait(max(options["poll"], 0.2))
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")
