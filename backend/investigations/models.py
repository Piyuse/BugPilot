import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .repository_urls import normalize_repository_url


class Repository(models.Model):
    name = models.CharField(max_length=200, unique=True)
    url = models.URLField(max_length=500, unique=True)
    default_branch = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    validation_commands = models.JSONField(default=list, blank=True)
    runtime_image = models.CharField(max_length=200, default="python:3.12-slim")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name="repository"
        verbose_name_plural="repositories"

    def clean(self):
        super().clean()
        try:
            self.url = normalize_repository_url(self.url)
        except ValueError as error:
            raise ValidationError({"url": str(error)}) from error

    def __str__(self):
        return self.name



class Investigation(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        PR_CREATED = "pr_created", "Ready for review"
        NEEDS_ATTENTION = "needs_attention", "Needs attention"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue_source = models.CharField(max_length=10, choices=[("github", "GitHub"), ("jira", "Jira")], default="github")
    source_repository_url = models.URLField(max_length=500, blank=True)
    source_issue_number = models.PositiveBigIntegerField(null=True, blank=True)
    issue_key = models.CharField(max_length=400)
    issue_url = models.URLField(max_length=700, blank=True)
    title = models.CharField(max_length=200, blank=True)
    requested_repository = models.ForeignKey(Repository, on_delete=models.PROTECT, null=True, blank=True, related_name="requests")
    repository = models.ForeignKey(Repository, on_delete=models.PROTECT, null=True, blank=True, related_name="investigations")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    mode = models.CharField(max_length=10, choices=[("live", "Live"), ("demo", "Demo")], default="live")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED, db_index=True)
    stage = models.CharField(max_length=30, default="queued")
    ticket = models.JSONField(default=dict, blank=True)
    preprocessed = models.JSONField(default=dict, blank=True)
    selection_reason = models.TextField(blank=True)
    branch = models.CharField(max_length=200, blank=True)
    base_branch = models.CharField(max_length=200, blank=True)
    base_sha = models.CharField(max_length=40, blank=True)
    publication_repository_url = models.URLField(max_length=500, blank=True)
    publication_head_owner = models.CharField(max_length=100, blank=True)
    publication_attempt = models.PositiveIntegerField(default=0)
    proposal = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    pull_request_url = models.URLField(max_length=700, blank=True)
    issue_linked = models.BooleanField(default=False)
    issue_comment_id = models.CharField(max_length=100, blank=True)
    issue_comment_url = models.URLField(max_length=700, blank=True)
    model_calls = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    attempt = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["issue_source", "issue_key", "mode"], name="one_investigation_per_source_ticket_mode")]


class InvestigationEvent(models.Model):
    investigation = models.ForeignKey(Investigation, on_delete=models.CASCADE, related_name="events")
    stage = models.CharField(max_length=30)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class WorkerHeartbeat(models.Model):
    updated_at = models.DateTimeField()
