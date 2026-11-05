"""Content-only scoping must not hide source, configuration, or test edits."""
import json
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from .integrations import IntegrationError
from .models import Repository
from .validation_profiles import node_runtime_additions, scoped_checks
from .validation_setup import detect_validation


class CurriculumValidationTests(SimpleTestCase):
    challenge = "curriculum/challenges/english/blocks/future-events/675329bc0e2bfa7f1c4f7411.md"
    regression = "curriculum/src/future-events.test.js"

    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "curriculum").mkdir()
        (self.workspace / "package.json").write_text(json.dumps({
            "name": "@freecodecamp/freecodecamp", "packageManager": "pnpm@12.15.0", "scripts": {"test": "turbo test"}}))
        (self.workspace / "curriculum/package.json").write_text(json.dumps({"name": "@freecodecamp/curriculum"}))
        self.repository = Repository(runtime_image="bugpilot-auto-10:prepared", validation_commands=[["pnpm", "test"]])

    def test_prepares_literal_locales_pinned_browser_and_dependency_builds(self):
        (self.workspace / "sample.env").write_text("AUTH0_CLIENT_SECRET=must-not-be-imported\nCURRICULUM_LOCALE=not-the-test-default\n")
        plan = detect_validation(self.workspace)
        self.assertIn("CURRICULUM_LOCALE=english", plan.dockerfile)
        self.assertIn("PUPPETEER_CACHE_DIR=/opt/bugpilot-browsers", plan.dockerfile)
        self.assertIn('"install-puppeteer"', plan.dockerfile)
        self.assertIn('"--concurrency=1"', plan.dockerfile)
        self.assertIn("libnss3", plan.dockerfile)
        self.assertNotIn("must-not-be-imported", plan.dockerfile)
        self.assertNotIn("AUTH0_CLIENT_SECRET", plan.dockerfile)
        self.assertEqual(plan.commands, [["pnpm", "test"]])

    def test_content_patch_runs_block_tooling_and_dependencies_sequentially(self):
        checks, scope = scoped_checks(self.repository, self.workspace, [self.challenge, self.regression], {self.regression})
        self.assertEqual(checks[0][:2], ["env", "FCC_BLOCK=future-events"])
        self.assertIn("--filter=@freecodecamp/curriculum...", checks[0])
        self.assertIn("--concurrency=1", checks[0])
        self.assertIn("--maxWorkers=2", checks[0])
        self.assertIn("--env-mode=loose", checks[0])
        self.assertIn("all curriculum tooling tests", scope)

    def test_source_config_existing_test_or_other_locale_edits_keep_full_checks(self):
        for other in ("curriculum/src/config.ts", self.regression, "turbo.json", "pnpm-lock.yaml",
                      self.challenge.replace("/english/", "/spanish/"), "api/src/index.ts"):
            with self.subTest(other=other):
                checks, scope = scoped_checks(self.repository, self.workspace, [self.challenge, other], set())
                self.assertEqual(checks, self.repository.validation_commands)
                self.assertEqual(scope, "")

    def test_custom_images_or_commands_are_preserved(self):
        for image, commands in (("company/custom:ready", [["pnpm", "test"]]),
                                ("bugpilot-auto-10:prepared", [["pnpm", "test", "--custom"]])):
            self.repository.runtime_image, self.repository.validation_commands = image, commands
            self.assertEqual(scoped_checks(self.repository, self.workspace, [self.challenge], set()), (commands, ""))

    def test_multiple_blocks_require_explicit_coverage(self):
        with self.assertRaisesMessage(IntegrationError, "multiple blocks"):
            scoped_checks(self.repository, self.workspace, [self.challenge, self.challenge.replace("future-events", "another-block")], set())

    def test_unrelated_node_project_does_not_inherit_repository_defaults(self):
        (self.workspace / "package.json").write_text(json.dumps({"name": "other", "scripts": {"test": "vitest run"}}))
        self.assertEqual(node_runtime_additions(self.workspace), "")
        self.assertEqual(scoped_checks(self.repository, self.workspace, [self.challenge], set()), (self.repository.validation_commands, ""))
