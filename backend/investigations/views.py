import shutil
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import Investigation, InvestigationEvent, Repository, WorkerHeartbeat
from .configuration import missing_live_settings, pipeline_configuration
from .pipeline import STAGES
from .serializers import (InvestigationDetailSerializer, InvestigationSerializer,
                          RepositorySerializer, SubmissionSerializer)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health_check(request):
    return Response({"application": "BugPilot", "status": "ok"})


class Pagination(PageNumberPagination):
    page_size = 20


class RegistryPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and (request.method in permissions.SAFE_METHODS or request.user.is_staff))


class RepositoryListCreateView(generics.ListCreateAPIView):
    queryset = Repository.objects.all()
    serializer_class = RepositorySerializer
    pagination_class = None
    permission_classes = [RegistryPermission]


class RepositoryDetailView(generics.RetrieveUpdateAPIView):
    queryset = Repository.objects.all()
    serializer_class = RepositorySerializer
    permission_classes = [RegistryPermission]


class InvestigationListView(generics.ListAPIView):
    serializer_class = InvestigationSerializer
    pagination_class = Pagination

    def get_queryset(self):
        queryset = Investigation.objects.select_related("repository", "requested_repository").filter(mode="live")
        value = self.request.query_params.get("status")
        if value in Investigation.Status.values:
            queryset = queryset.filter(status=value)
        search = self.request.query_params.get("q", "").strip()[:200]
        if search:
            from django.db.models import Q
            queryset = queryset.filter(Q(issue_key__icontains=search) | Q(title__icontains=search))
        return queryset

    def post(self, request):
        serializer = SubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key = serializer.validated_data["issue_key"]
        source = serializer.validated_data["issue_source"]
        mode = serializer.validated_data["mode"]
        existing = Investigation.objects.filter(issue_source=source, issue_key=key, mode=mode).first()
        if existing:
            return Response(InvestigationDetailSerializer(existing).data)
        if missing := missing_live_settings(source):
            return Response({"configuration": "Configure backend/.env and restart the backend and worker. Missing: " + ", ".join(missing)}, status=400)
        try:
            with transaction.atomic():
                run, created = Investigation.objects.get_or_create(issue_source=source, issue_key=key, mode=mode, defaults={
                    "submitted_by": request.user,
                    "requested_repository": serializer.validated_data.get("repository"),
                    "issue_url": serializer.validated_data["issue_url"],
                    "source_repository_url": serializer.validated_data.get("source_repository_url", ""),
                    "source_issue_number": serializer.validated_data.get("source_issue_number"),
                })
                if created:
                    InvestigationEvent.objects.create(investigation=run, stage="queued", message="Ticket accepted. Waiting for the background worker.")
        except IntegrityError:
            run, created = Investigation.objects.get(issue_source=source, issue_key=key, mode=mode), False
        return Response(InvestigationDetailSerializer(run).data, status=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK)


class InvestigationDetailView(generics.RetrieveAPIView):
    queryset = Investigation.objects.select_related("repository", "requested_repository").prefetch_related("events").filter(mode="live")
    serializer_class = InvestigationDetailSerializer


@api_view(["POST"])
def retry_investigation(request, pk):
    # A conditional update prevents two simultaneous retries from queuing duplicate work.
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(Investigation, pk=pk, mode="live")
    if not run.pull_request_url and (missing := missing_live_settings(run.issue_source)):
        return Response({"configuration": "Missing live settings: " + ", ".join(missing)}, status=400)
    changes = {}
    if request.data.get("repository"):
        issue_input = {"github_issue": run.issue_url} if run.issue_source == "github" else {"jira_ticket": run.issue_key}
        serializer = SubmissionSerializer(data={**issue_input, "repository": request.data["repository"], "mode": run.mode})
        serializer.is_valid(raise_exception=True)
        if run.pull_request_url or run.stage in {"publishing", "linking_jira", "linking_issue"}:
            return Response({"detail": "The publication step has started. Retry with the same repository to recover its pull request."}, status=409)
        changes = {"requested_repository": serializer.validated_data["repository"], "repository": None, "base_branch": "", "base_sha": ""}
    if not Investigation.objects.filter(pk=pk, status="needs_attention").update(status="queued", stage="queued", error="", finished_at=None, updated_at=timezone.now(), **changes):
        return Response({"detail": "Only investigations that need attention can be retried."}, status=409)
    InvestigationEvent.objects.create(investigation=run, stage="queued", message="Retry queued. Any existing pull request will be recovered before continuing.")
    run.refresh_from_db()
    return Response(InvestigationDetailSerializer(run).data, status=202)


@api_view(["GET"])
def overview(request):
    counts = dict(Investigation.objects.filter(mode="live").values("status").annotate(total=Count("id")).values_list("status", "total"))
    heartbeat = WorkerHeartbeat.objects.filter(pk=1).first()
    worker_online = bool(heartbeat and heartbeat.updated_at > timezone.now() - timedelta(seconds=35))
    providers = [
        {"name": "OpenAI", "description": "Preprocess & locate repositories", "configured": bool(settings.OPENAI_API_KEY and settings.OPENAI_PREPROCESS_MODEL)},
        {"name": "GitHub", "description": "Read issues, publish PRs & comment fix links", "configured": bool(settings.GITHUB_TOKEN)},
        {"name": "Codex", "description": "Investigate & implement fixes", "configured": bool(settings.CODEX_API_KEY and shutil.which(settings.CODEX_BIN))},
        {"name": "Docker", "description": "Isolated regression checks", "configured": bool(shutil.which("docker"))},
    ]
    return Response({
        "mode": "live", "counts": counts, "worker_online": worker_online,
        "worker_last_seen": heartbeat.updated_at if heartbeat else None,
        "stages": [{"key": key, "label": label} for key, label in STAGES],
        "integrations": providers, "username": request.user.username, "can_manage": request.user.is_staff,
        "pipeline": pipeline_configuration(),
    })
