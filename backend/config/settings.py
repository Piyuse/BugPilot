"""BugPilot configuration. Copy .env.example to backend/.env for local setup."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Minimal dotenv reader; real environment variables always take precedence.
env_file = BASE_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

DEBUG = os.getenv("DEBUG", "true").lower() == "true"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-bugpilot-change-before-deploying" if DEBUG else "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DEBUG=false.")
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "rest_framework", "investigations",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": os.getenv("BUGPILOT_DB_PATH", str(BASE_DIR / "db.sqlite3")), "OPTIONS": {"timeout": 30}}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = []
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "/admin/login/"
BUGPILOT_FRONTEND_URL = os.getenv("BUGPILOT_FRONTEND_URL", "http://127.0.0.1:5173")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if os.getenv("TRUST_PROXY", "false").lower() == "true" else None

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "300/minute"},
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# Preserve the workspace root introduced by the original project.
BUGPILOT_WORKSPACE_ROOT = Path(os.getenv("BUGPILOT_WORKSPACE_ROOT", str(BASE_DIR.parent / "workspaces")))
BUGPILOT_AGENT_TIMEOUT = int(os.getenv("BUGPILOT_AGENT_TIMEOUT", "1200"))
BUGPILOT_TEST_TIMEOUT = int(os.getenv("BUGPILOT_TEST_TIMEOUT", "300"))
BUGPILOT_CONTAINER_SETUP_TIMEOUT = int(os.getenv("BUGPILOT_CONTAINER_SETUP_TIMEOUT", "900"))
BUGPILOT_TEST_MEMORY = os.getenv("BUGPILOT_TEST_MEMORY", "2g")
BUGPILOT_VALIDATION_SETUP_TIMEOUT = int(os.getenv("BUGPILOT_VALIDATION_SETUP_TIMEOUT", "1200"))
BUGPILOT_GIT_NETWORK_TIMEOUT = int(os.getenv("BUGPILOT_GIT_NETWORK_TIMEOUT", "600"))
BUGPILOT_LEASE_SECONDS = int(os.getenv("BUGPILOT_LEASE_SECONDS", "1800"))
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "") or os.getenv("CODEX_API_KEY", "")
OPENAI_PREPROCESS_MODEL = os.getenv("OPENAI_PREPROCESS_MODEL", "gpt-6-luna")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
CODEX_API_KEY = os.getenv("CODEX_API_KEY", "") or OPENAI_API_KEY
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CODEX_MODEL = os.getenv("CODEX_MODEL", "")
BUGPILOT_WINDOWS_SANDBOX = os.getenv("BUGPILOT_WINDOWS_SANDBOX", "mxc")
if BUGPILOT_WINDOWS_SANDBOX not in {"mxc", "elevated", "unelevated"}:
    raise ImproperlyConfigured("BUGPILOT_WINDOWS_SANDBOX must be mxc, elevated or unelevated.")
