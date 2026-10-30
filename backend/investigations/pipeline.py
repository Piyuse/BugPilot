"""Durable, checkpointed ticket-to-PR orchestration used by the worker."""
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import execution
from .integrations import OpenAIPreprocessor, GitHubClient, IntegrationError, JiraClient
from .models import Investigation, InvestigationEvent, Repository
from .configuration import missing_live_settings
from .validation_setup import automatic_validation, prepare_automatic_validation


STAGES = [
    ("fetching_ticket", "Read GitHub issue"),
    ("preprocessing", "Structure bug context"),
    ("selecting_repository", "Locate affected repository"),
    ("preparing_workspace", "Create fix branch"),
    ("preparing_validation", "Prepare test environment"),
    ("fixing", "Investigate and fix"),
    ("validating", "Run regression checks"),
    ("publishing", "Open pull request"),
    ("linking_issue", "Comment PR link on issue"),
]


def stage(run, key, message):
    if not Investigation.objects.filter(pk=run.pk, status="running", attempt=run.attempt).exists():
        raise IntegrationError("This worker no longer owns the investigation.")
    run.stage = key
    run.save(update_fields=["stage", "updated_at"])
    InvestigationEvent.objects.create(investigation=run, stage=key, message=message)


def select_repository(run, openai, github):
    if run.requested_repository_id:
        if not run.requested_repository.is_active or run.requested_repository.is_demo:
            raise IntegrationError("The selected repository is inactive or is a demo fixture.")
        return run.requested_repository, "Selected by the developer."
    repositories = list(Repository.objects.filter(is_active=True).exclude(url__startswith="https://github.com/bugpilot-demo/"))
    if not repositories:
        raise IntegrationError("Register at least one active, real repository in Repositories. Demo fixtures cannot be used for live runs.")
    if run.issue_source == "github":
        hosting_repository = next((repo for repo in repositories if repo.url == run.source_repository_url), None)
        if hosting_repository:
            return hosting_repository, "Inferred from the GitHub issue URL."
    if len(repositories) > 30:
        raise IntegrationError("More than 30 repositories are active. Select a repository or narrow the registry.")
    if len(repositories) == 1:
        return repositories[0], "Only one active repository is registered."
    contexts = [github.context(repository) for repository in repositories]
    selection = openai.select(run.preprocessed, contexts, on_retry=lambda message: stage(run, "selecting_repository", "OpenAI: " + message))
    return next(repo for repo in repositories if repo.pk == selection["repository_id"]), selection["reason"]


def complete(run):
    run.status = Investigation.Status.PR_CREATED
    run.stage = "completed"
    run.error = ""
    run.finished_at = timezone.now()
    if not Investigation.objects.filter(pk=run.pk, status="running", attempt=run.attempt).update(
        status=run.status, stage=run.stage, error="", finished_at=run.finished_at, updated_at=timezone.now()
    ):
        raise IntegrationError("This worker no longer owns the investigation.")
    InvestigationEvent.objects.create(investigation=run, stage="completed", message=(
        "Demo complete. No external services, branches, PRs or issue comments were created."
        if run.mode == "demo" else f"Pull request is ready for review and its URL was posted as a {run.issue_source.title()} issue comment."
    ))


def fix_branch(run):
    reference = f"gh-{run.source_issue_number}" if run.issue_source == "github" else run.issue_key.lower()
    return f"codex/{reference}-{run.pk.hex[:10]}"


def publish_validated_fix(run, workspace, github):
    run.publication_attempt = run.publication_attempt or run.attempt
    run.save(update_fields=["publication_attempt", "updated_at"])
    stage(run, "publishing", "Checks passed. Publishing the fix branch to an accessible repository and opening a PR against the original repository.")
    pull_request = execution.publish_pull_request(run, workspace, github)
    run.pull_request_url = pull_request["html_url"]
    run.save(update_fields=["pull_request_url", "updated_at"])


def run_live(run):
    jira = JiraClient() if run.issue_source == "jira" else None
    if not run.branch:
        run.branch = fix_branch(run)
        run.save(update_fields=["branch", "updated_at"])

    github = None
    if not run.pull_request_url and run.repository_id:
        github = GitHubClient()
        existing = github.existing_pull_request(run.repository, run.branch, head_owner=run.publication_head_owner or None)
        if existing:
            run.pull_request_url = existing["html_url"]
            run.save(update_fields=["pull_request_url", "updated_at"])

    if not run.pull_request_url and run.publication_attempt:
        if not run.repository_id or not run.base_sha:
            raise IntegrationError("The publication checkpoint has no validated fix. No code was pushed.")
        workspace = Path(settings.BUGPILOT_WORKSPACE_ROOT) / str(run.pk) / f"attempt-{run.publication_attempt}"
        if not workspace.is_dir():
            raise IntegrationError("The validated publication workspace is missing. Restore it before retrying; no new model run was started.")
        stage(run, "validating", "Recovering the existing fix and rechecking its source and tests before retrying publication.")
        try:
            run.validation = execution.validate_proposal(run, workspace)
            if run.validation.get("passed") is not True:
                raise IntegrationError("The recovered fix did not pass validation. No code was pushed.")
        except IntegrationError as error:
            run.validation = {"passed": False, "reason": execution.redact(str(error))}
            run.save(update_fields=["validation", "updated_at"])
            raise
        run.save(update_fields=["validation", "updated_at"])
        publish_validated_fix(run, workspace, github)

    if not run.pull_request_url:
        missing = missing_live_settings(run.issue_source)
        if missing:
            raise IntegrationError("Configure backend/.env before a live run: " + ", ".join(missing))
        execution.ensure_coding_runtime()
        github = github or GitHubClient()
        openai = OpenAIPreprocessor()
        stage(run, "fetching_ticket", f"Reading the {run.issue_source.title()} issue title, description and labels. Personal fields are not retained.")
        run.ticket = github.issue(run.source_repository_url, run.source_issue_number) if run.issue_source == "github" else jira.ticket(run.issue_key)
        run.title = run.ticket["title"]
        run.issue_url = run.ticket["url"]
        run.save(update_fields=["ticket", "title", "issue_url", "updated_at"])
        stage(run, "preprocessing", f"Calling OpenAI ({settings.OPENAI_PREPROCESS_MODEL}) to extract concise bug context and exact technical evidence.")
        run.preprocessed = openai.preprocess(run.ticket, on_retry=lambda message: stage(run, "preprocessing", "OpenAI: " + message))
        run.model_calls["preprocessing"] = {
            "provider": "openai", "model": settings.OPENAI_PREPROCESS_MODEL,
            "completed_at": timezone.now().isoformat(),
        }
        run.save(update_fields=["preprocessed", "model_calls", "updated_at"])
        stage(run, "selecting_repository", "Matching the processed bug to the real repository allowlist.")
        run.repository, run.selection_reason = select_repository(run, openai, github)
        if not run.base_branch:
            run.base_branch = github.default_branch(run.repository)
            run.repository.default_branch = run.base_branch
            run.repository.save(update_fields=["default_branch"])
        run.save(update_fields=["repository", "selection_reason", "base_branch", "updated_at"])
        auto_validation = automatic_validation(run.repository)
        if not auto_validation:
            execution.ensure_validation_runtime(run.repository)
        stage(run, "preparing_workspace", f"Creating {run.branch} from {run.repository.name}'s default branch, {run.base_branch}.")
        workspace = execution.prepare_workspace(run, on_progress=lambda message: stage(run, "preparing_workspace", message))
        if auto_validation:
            stage(run, "preparing_validation", "Detecting tests and preparing their Docker environment before coding.")
            prepare_automatic_validation(run.repository, workspace, on_progress=lambda message: stage(run, "preparing_validation", message))
        coding_model = settings.CODEX_MODEL or "Codex CLI default"
        stage(run, "fixing", f"Calling Codex ({coding_model}) with the OpenAI-processed brief and repository checkout to diagnose, fix and add regression coverage.")
        initial_head = execution.git(["rev-parse", "HEAD"], cwd=workspace)
        run.proposal = execution.investigate_and_fix(run, workspace)
        if execution.git(["rev-parse", "HEAD"], cwd=workspace) != initial_head:
            raise IntegrationError("The agent changed Git history. The patch needs manual review.")
        run.model_calls["coding"] = {
            "provider": "codex", "model": coding_model,
            "completed_at": timezone.now().isoformat(),
        }
        run.save(update_fields=["proposal", "model_calls", "updated_at"])
        stage(run, "validating", "Running configured checks in resource-limited containers without network access.")
        try:
            run.validation = execution.validate_proposal(run, workspace)
            if run.validation.get("passed") is not True:
                raise IntegrationError(run.validation.get("reason") or "Validation did not pass. No pull request was created.")
        except IntegrationError as error:
            run.validation = {"passed": False, "reason": execution.redact(str(error))}
            run.save(update_fields=["validation", "updated_at"])
            raise
        run.save(update_fields=["validation", "updated_at"])
        publish_validated_fix(run, workspace, github)

    if not run.issue_comment_id:
        stage(run, "linking_issue", f"Posting the PR link as a comment on the original {run.issue_source.title()} issue; existing comments are recovered on retry.")
        details = {
            "investigation_id": str(run.pk), "branch": run.branch,
            "summary": run.proposal.get("summary", ""), "validation": run.validation,
        }
        title = f"{run.issue_key}: {run.title}"
        if run.issue_source == "github":
            github = github or GitHubClient()
            comment = github.comment_issue_pull_request(run.source_repository_url, run.source_issue_number, run.pull_request_url, title, **details)
        else:
            comment = jira.comment_pull_request(run.issue_key, run.pull_request_url, title, **details)
        run.issue_comment_id = comment["id"]
        run.issue_comment_url = comment["url"]
    run.issue_linked = True
    run.save(update_fields=["issue_linked", "issue_comment_id", "issue_comment_url", "updated_at"])
    complete(run)


def run_demo(run):
    """Deterministic local fixture; deliberately performs no external actions."""
    repositories = list(Repository.objects.filter(is_active=True))
    if run.requested_repository_id and not run.requested_repository.is_active:
        raise IntegrationError("The selected repository is inactive or is a demo fixture.")
    if not repositories:
        raise IntegrationError("Register a repository, or run the seed_demo management command.")
    for key, label in STAGES:
        stage(run, key, f"[Demo simulation] {label}.")
        if key == "fetching_ticket":
            run.title = "Checkout fails when a discount expires during payment"
            run.ticket = {"title": run.title, "description": "An expired promotion returns None from the discount lookup. Checkout accesses discount.amount and returns HTTP 500 instead of charging the normal price.", "priority": "High", "labels": ["checkout", "payments"], "components": ["Checkout"]}
        elif key == "preprocessing":
            run.preprocessed = {
                "summary": "Checkout dereferences an expired discount without a null check.",
                "expected_behavior": "Complete checkout at the normal price when a discount expires.",
                "actual_behavior": "HTTP 500 while reading discount.amount.",
                "reproduction_steps": ["Add a discounted product to the cart.", "Let the promotion expire.", "Complete payment."],
                "error_details": "AttributeError: 'NoneType' object has no attribute 'amount'",
                "keywords": ["checkout", "discount", "payment", "expiration"],
            }
        elif key == "selecting_repository":
            run.repository = run.requested_repository or next((repo for repo in repositories if "checkout" in repo.name.lower()), repositories[0])
            run.selection_reason = "Demo fixture matches checkout and discount symptoms to the checkout service." if not run.requested_repository_id else "Selected by the developer."
        elif key == "preparing_workspace":
            run.branch = fix_branch(run)
        elif key == "fixing":
            run.proposal = {
                "summary": "Handle an expired promotion by falling back to the normal checkout total.",
                "root_cause": "The discount lookup can return None after a promotion expires, but checkout assumes a discount always exists.",
                "tests_added": ["Expired promotions use the normal price.", "Active promotions still apply the discount."],
                "diff": "--- a/checkout/pricing.py\n+++ b/checkout/pricing.py\n@@ -18,2 +18,2 @@\n-    total = subtotal - discount.amount\n+    total = subtotal - (discount.amount if discount else 0)\n     return max(total, 0)\n",
            }
        elif key == "validating":
            run.validation = {"passed": True, "simulated": True, "checks": [{"command": ["python", "-m", "pytest", "tests/test_checkout.py"], "passed": True, "output": "Demo fixture: 12 simulated tests passed. No tests were executed."}], "changed_files": ["checkout/pricing.py", "tests/test_checkout.py"]}
        run.save(update_fields=["title", "ticket", "preprocessed", "repository", "selection_reason", "branch", "proposal", "validation", "updated_at"])
        time.sleep(settings.BUGPILOT_DEMO_STAGE_DELAY)
    complete(run)


def run_investigation(investigation_id):
    run = Investigation.objects.select_related("requested_repository", "repository").get(pk=investigation_id)
    if run.status != Investigation.Status.RUNNING:
        return {"status": run.status, "pull_request_url": run.pull_request_url or None}
    try:
        (run_demo if run.mode == "demo" else run_live)(run)
    except Exception as error:
        # Never expose unexpected exception details (which could contain provider credentials).
        message = execution.redact(str(error))[:10000] if isinstance(error, IntegrationError) else "An unexpected worker error occurred. Check the worker logs and retry."
        run.status = Investigation.Status.NEEDS_ATTENTION
        run.error = message
        run.finished_at = timezone.now()
        owned = Investigation.objects.filter(pk=run.pk, status="running", attempt=run.attempt).update(
            status=run.status, error=message, finished_at=run.finished_at, updated_at=timezone.now()
        )
        if owned:
            InvestigationEvent.objects.create(investigation=run, stage=run.stage, message=message)
        else:
            run.refresh_from_db()
        if not isinstance(error, IntegrationError):
            import logging
            logging.getLogger(__name__).error("Unexpected worker failure for %s (%s)", run.pk, type(error).__name__)
    return {"status": run.status, "pull_request_url": run.pull_request_url or None}
