from django.conf import settings

REQUIRED_LIVE_SETTINGS = (
    "OPENAI_API_KEY", "OPENAI_PREPROCESS_MODEL", "GITHUB_TOKEN", "CODEX_API_KEY",
)


def missing_live_settings(issue_source="github"):
    required = REQUIRED_LIVE_SETTINGS + (("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if issue_source == "jira" else ())
    return [name for name in required if not getattr(settings, name, "")]


def pipeline_configuration():
    missing = missing_live_settings()
    return {
        "ready": not missing,
        "missing": missing,
        "preprocessing": {"provider": "openai", "model": settings.OPENAI_PREPROCESS_MODEL},
        "coding": {"provider": "codex", "model": settings.CODEX_MODEL or "Codex CLI default"},
    }
