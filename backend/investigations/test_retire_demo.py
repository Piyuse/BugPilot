"""Upgrade coverage: historical simulations must never become live jobs."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RetireDemoMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_history_and_live_publication_checkpoints(self):
        old = [("investigations", "0009_remove_bugreport")]
        new = [("investigations", "0010_retire_demo_mode")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old)
            apps = executor.loader.project_state(old).apps
            Repository = apps.get_model("investigations", "Repository")
            Investigation = apps.get_model("investigations", "Investigation")
            sample = Repository.objects.create(name="sample", url="https://github.com/bugpilot-demo/checkout-service")
            real = Repository.objects.create(name="project", url="https://github.com/acme/project")
            historical = Investigation.objects.create(issue_key="acme/project#1", mode="demo", requested_repository=sample)
            live = Investigation.objects.create(
                issue_key="acme/project#1", mode="live", repository=real,
                status="pr_created", pull_request_url="https://github.com/acme/project/pull/2",
                publication_head_owner="contributor", publication_attempt=2,
                base_branch="trunk", issue_comment_id="42",
            )
            executor = MigrationExecutor(connection)
            executor.migrate(new)
            apps = executor.loader.project_state(new).apps
            Repository = apps.get_model("investigations", "Repository")
            Investigation = apps.get_model("investigations", "Investigation")
            self.assertFalse(Repository.objects.get(pk=sample.pk).is_active)
            self.assertTrue(Repository.objects.get(pk=real.pk).is_active)
            self.assertEqual(Investigation.objects.get(pk=historical.pk).mode, "demo")
            saved = Investigation.objects.get(pk=live.pk)
            self.assertEqual(saved.repository_id, real.pk)
            self.assertEqual(saved.pull_request_url, "https://github.com/acme/project/pull/2")
            self.assertEqual(saved.publication_head_owner, "contributor")
            self.assertEqual(saved.publication_attempt, 2)
            self.assertEqual(saved.base_branch, "trunk")
            self.assertEqual(saved.issue_comment_id, "42")
            self.assertEqual(saved.status, "pr_created")
        finally:
            MigrationExecutor(connection).migrate(latest)
