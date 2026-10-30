"""Session authentication for the separately served TypeScript frontend."""
import json

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST


def session_payload(request):
    authenticated = request.user.is_authenticated
    return {
        "authenticated": authenticated,
        "username": request.user.username if authenticated else "",
        "can_manage": bool(authenticated and request.user.is_staff),
        "mode": settings.BUGPILOT_MODE,
    }


@require_GET
@ensure_csrf_cookie
def session_view(request):
    # Also issues the initial CSRF cookie before unauthenticated login.
    return JsonResponse(session_payload(request))


@require_POST
@csrf_protect
def login_view(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError):
        return JsonResponse({"detail": "Send a JSON object with username and password."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"detail": "Send a JSON object with username and password."}, status=400)
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password or len(username) > 150 or len(password) > 4096:
        return JsonResponse({"detail": "Enter a valid username and password."}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"detail": "Your username or password is incorrect. Try again."}, status=400)
    login(request, user)
    return JsonResponse(session_payload(request))


@require_POST
@csrf_protect
def logout_view(request):
    logout(request)
    return JsonResponse(session_payload(request))


@require_GET
def backend_info(request):
    return JsonResponse({
        "application": "BugPilot API",
        "frontend": settings.BUGPILOT_FRONTEND_URL,
        "health": "/api/health/",
    })
