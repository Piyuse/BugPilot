import io
import json
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from .integrations import IntegrationError
from .models import Investigation, Repository
from .pipeline import run_investigation
from .tests import LIVE_SETTINGS
from .validation_setup import automatic_validation, build_context, build_test_image, detect_validation, prepare_automatic_validation, transient_builder_error


class DetectionTests(SimpleTestCase):
    def setUp(self):
        root = settings.BASE_DIR / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)

    def write(self, name, content):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_node_uses_locked_dependencies_and_declared_manager(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "pnpm@10.12.1"}))
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'")
        self.write(".nvmrc", "24")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["pnpm", "test"]])
        self.assertIn("node:24-bookworm", plan.dockerfile)
        self.assertIn('"pnpm@10.12.1"', plan.dockerfile)
        self.assertIn('["pnpm", "install", "--frozen-lockfile"]', plan.dockerfile)

    def test_npm_test_and_literal_python_ci_dependencies_are_detected(self):
        self.write("package.json", json.dumps({"scripts": {"test": "node --test tests/*.test.js"}}))
        self.write("package-lock.json", "{}")
        self.write(".github/workflows/test.yml", "run: pip install pandas\nrun: pip install $SECRET\nrun: pip install ok && curl evil\n")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["npm", "test"]])
        self.assertIn('"npm", "ci"', plan.dockerfile)
        self.assertIn('"pandas"', plan.dockerfile)
        self.assertIn("FROM python:3.12-bookworm", plan.dockerfile)
        self.assertNotIn("SECRET", plan.dockerfile)
        self.assertNotIn("curl", plan.dockerfile)

    def test_modern_yarn_is_prepared_through_corepack_with_an_offline_cache(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "yarn@4.9.2"}))
        self.write("yarn.lock", "# lockfile")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["yarn", "test"]])
        self.assertIn('["corepack", "install", "--global", "yarn@4.9.2"]', plan.dockerfile)
        self.assertIn('"--immutable"', plan.dockerfile)
        self.assertIn("COREPACK_HOME=/work/", plan.dockerfile)

    def test_placeholder_or_missing_test_script_does_not_count_as_validation(self):
        for script in (None, "", 'echo "Error: no test specified" && exit 1', "exit 0", "echo success"):
            with self.subTest(script=script):
                self.write("package.json", json.dumps({"scripts": {"test": script}}))
                with self.assertRaisesMessage(IntegrationError, "no runnable test script"):
                    detect_validation(self.workspace)

    def test_python_installs_project_test_extra_and_requirements(self):
        self.write("pyproject.toml", '[project]\nname="example"\nversion="1.0"\n[project.optional-dependencies]\ntest=["pytest-asyncio"]\n')
        self.write("requirements.txt", "requests==2.32.0")
        self.write(".python-version", "3.13.2")
        plan = detect_validation(self.workspace)
        self.assertEqual(plan.commands, [["python", "-m", "pytest"]])
        self.assertIn("python:3.13.2-bookworm", plan.dockerfile)
        self.assertIn('"-r", "requirements.txt"', plan.dockerfile)
        self.assertIn('"-e", ".[test]"', plan.dockerfile)

    def test_unknown_setup_is_not_silently_marked_passing(self):
        self.write("README.md", "a project")
        with self.assertRaisesMessage(IntegrationError, "could not detect"):
            detect_validation(self.workspace)

    def test_build_context_filters_secrets_and_replaces_untrusted_dockerfile(self):
        snapshot = self.workspace / "source.tar"
        target = self.workspace / "context.tar"
        with tarfile.open(snapshot, "w") as archive:
            for name in ("test.js", ".env", "nested/.env.production", ".npmrc", "key.pem", ".git/config", ".dockerignore", ".bugpilot-runtime/Dockerfile"):
                data = b"untrusted"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        build_context(snapshot, target, "FROM trusted\n")
        # Docker must identify this as an archive even when Git supplies PAX headers.
        self.assertEqual(target.read_bytes()[:2], b"\x1f\x8b")
        with tarfile.open(target) as archive:
            self.assertEqual(set(archive.getnames()), {"test.js", ".dockerignore", ".bugpilot-runtime/Dockerfile"})
            self.assertEqual(archive.extractfile(".bugpilot-runtime/Dockerfile").read(), b"FROM trusted\n")

    def test_build_context_rejects_symlink_to_host_files(self):
        snapshot = self.workspace / "source.tar"
        with tarfile.open(snapshot, "w") as archive:
            info = tarfile.TarInfo("outside")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaisesMessage(IntegrationError, "symlink"):
            build_context(snapshot, self.workspace / "context.tar", "FROM trusted\n")


@override_settings(**LIVE_SETTINGS)
class AutomaticValidationPipelineTests(TestCase):
    def setUp(self):
        self.repository = Repository.objects.create(name="web", url="https://github.com/acme/web", validation_commands=[])
        self.run = Investigation.objects.create(issue_key="acme/web#1", source_repository_url=self.repository.url, source_issue_number=1,
            requested_repository=self.repository, status="running", attempt=1)

    def test_registry_accepts_automatic_setup_without_image_or_commands(self):
        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user("owner", is_staff=True))
        result = client.patch(f"/api/repositories/{self.repository.pk}/", {"runtime_image": "", "validation_commands": []}, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["validation_commands"], [])
        result = client.patch(f"/api/repositories/{self.repository.pk}/", {"runtime_image": "", "validation_commands": [["npm", "test"]]}, format="json")
        self.assertEqual(result.status_code, 400)

    @patch("investigations.pipeline.prepare_automatic_validation")
    @patch("investigations.pipeline.execution")
    @patch("investigations.pipeline.OpenAIPreprocessor")
    @patch("investigations.pipeline.GitHubClient")
    def test_empty_commands_trigger_preparation_before_agent_and_still_require_tests(self, github, model, executor, prepare):
        calls = []
        github.return_value.default_branch.return_value = "trunk"
        github.return_value.issue.return_value = {"title": "Crash", "url": self.repository.url + "/issues/1"}
        model.return_value.preprocess.return_value = {"summary": "Crash"}
        prepare.side_effect = lambda *a, **kw: calls.append("prepare")
        executor.investigate_and_fix.side_effect = lambda *a: calls.append("agent") or {"summary": "fix"}
        executor.validate_proposal.side_effect = IntegrationError("A regression failed")
        executor.redact.side_effect = lambda text: text
        run_investigation(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(calls, ["prepare", "agent"])
        self.assertIn("preparing_validation", list(self.run.events.values_list("stage", flat=True)))
        self.assertEqual(self.run.status, "needs_attention")
        self.assertEqual(self.run.error, "A regression failed")
        executor.publish_pull_request.assert_not_called()
        github.return_value.comment_issue_pull_request.assert_not_called()

    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command", return_value="linux")
    @patch("investigations.validation_setup.detect_validation")
    def test_ready_image_is_cached_and_saved_but_remains_automatic(self, detect, command, git):
        from .validation_setup import ValidationPlan
        detect.return_value = ValidationPlan("Node", [["npm", "test"]], "FROM node:22-bookworm\n")
        prepare_automatic_validation(self.repository, Path("unused"))
        self.repository.refresh_from_db()
        self.assertTrue(automatic_validation(self.repository))
        self.assertEqual(self.repository.validation_commands, [["npm", "test"]])
        self.assertEqual(command.call_count, 1)

    @patch("investigations.validation_setup.build_context")
    @patch("investigations.validation_setup.execution.git", return_value="a" * 40)
    @patch("investigations.validation_setup.execution.command", side_effect=[IntegrationError("missing"), "ready", IntegrationError("install failed")])
    @patch("investigations.validation_setup.detect_validation")
    def test_failed_image_build_does_not_save_fake_ready_configuration(self, detect, command, git, context):
        from .validation_setup import ValidationPlan
        detect.return_value = ValidationPlan("Node", [["npm", "test"]], "FROM node:22-bookworm\n")
        with tempfile.TemporaryDirectory(dir=settings.BASE_DIR / ".test-tmp") as root, override_settings(BUGPILOT_WORKSPACE_ROOT=Path(root)):
            with self.assertRaisesMessage(IntegrationError, "install failed"):
                prepare_automatic_validation(self.repository, Path(root))
        self.repository.refresh_from_db()
        self.assertEqual(self.repository.validation_commands, [])
        self.assertEqual(self.repository.runtime_image, "python:3.12-slim")
        self.assertEqual(command.call_args.args[0][1], "build")
        self.assertIsInstance(command.call_args.kwargs["stdin"], Path)

    def test_manual_configuration_is_preserved(self):
        self.repository.validation_commands = [["custom", "check"]]
        self.repository.runtime_image = "team/prepared:ready"
        self.assertFalse(automatic_validation(self.repository))


class BuilderRecoveryTests(SimpleTestCase):
    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_resource_saver_disconnect_rechecks_builder_and_retries_same_archive(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("listing workers for Build: failed to list workers: Unavailable: error reading from server: EOF"), "ready", "built"]
        progress = []
        context = Path("context.tar.gz")
        build_test_image("bugpilot-auto-1:test", context, on_progress=progress.append)
        builds = [call for call in command.call_args_list if call.args[0][1] == "build"]
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(call.kwargs["stdin"] == context for call in builds))
        self.assertEqual([call.args[0][1:3] for call in command.call_args_list], [["buildx", "inspect"], ["build", "--file"], ["buildx", "inspect"], ["build", "--file"]])
        self.assertIn("2/3", progress[0])

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_starting_builder_is_waited_for(self, command, sleep):
        command.side_effect = [IntegrationError("rpc error: code = Unavailable"), "ready", "built"]
        build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 3)
        sleep.assert_called_once()

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_repeated_disconnects_stop_after_three_builds(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("error reading from server: EOF"), "ready", IntegrationError("error reading from server: EOF"), "ready", IntegrationError("error reading from server: EOF")]
        with self.assertRaisesMessage(IntegrationError, "all 3 attempts"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(len([call for call in command.call_args_list if call.args[0][1] == "build"]), 3)

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    def test_package_install_failure_is_not_retried(self, command, sleep):
        command.side_effect = ["ready", IntegrationError("npm ci: lockfile does not match package.json")]
        with self.assertRaisesMessage(IntegrationError, "lockfile"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 2)
        sleep.assert_not_called()

    @patch("investigations.validation_setup.time.sleep")
    @patch("investigations.validation_setup.execution.command")
    @patch("investigations.validation_setup.time.monotonic")
    @override_settings(BUGPILOT_VALIDATION_SETUP_TIMEOUT=10)
    def test_retries_share_one_setup_deadline(self, monotonic, command, sleep):
        monotonic.side_effect = [0, 0, 0, 0, 11, 11, 11]
        command.side_effect = ["ready", IntegrationError("error reading from server: EOF")]
        with self.assertRaisesMessage(IntegrationError, "setup time limit"):
            build_test_image("bugpilot-auto-1:test", Path("context.tar.gz"))
        self.assertEqual(command.call_count, 2)

    def test_permission_and_recipe_errors_are_not_transport_failures(self):
        for message in ("permission denied connecting to Docker", "access is denied", "Dockerfile parse error: unexpected EOF", "npm ERR! 401 Unauthorized", "RUN pip install exited with code 1"):
            self.assertFalse(transient_builder_error(IntegrationError(message)))
