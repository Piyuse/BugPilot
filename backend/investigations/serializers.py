import re

from django.conf import settings
from django.db.models import Q
from rest_framework import serializers

from .models import Investigation, InvestigationEvent, Repository
from .repository_urls import normalize_repository_url, parse_ticket, parse_github_issue
from .task_context import coding_brief


def validate_commands(value):
    if not isinstance(value, list) or len(value) > 10:
        raise serializers.ValidationError("Provide at most 10 command argument arrays.")
    for command in value:
        if not isinstance(command, list) or not command or len(command) > 50 or any(not isinstance(arg, str) or not arg or len(arg) > 1000 or "\x00" in arg for arg in command):
            raise serializers.ValidationError('Commands must be argument arrays, for example [["python", "-m", "pytest"]].')
    return value


class RepositorySerializer(serializers.ModelSerializer):
    runtime_image = serializers.CharField(max_length=200, allow_blank=True, required=False)
    class Meta:
        model = Repository
        fields = ["id", "name", "url", "default_branch", "description", "is_active", "validation_commands", "runtime_image", "created_at"]
        read_only_fields = ["id", "created_at", "default_branch"]
        extra_kwargs = {"description": {"max_length": 10000}}

    def validate_name(self, value):
        existing = Repository.objects.filter(name__iexact=value)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A repository with this name is already registered.")
        return value

    def validate_url(self, value):
        try:
            normalized = normalize_repository_url(value)
        except ValueError as error:
            raise serializers.ValidationError(str(error)) from error
        existing = Repository.objects.filter(url=normalized)
        if self.instance:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("This repository is already registered.")
        return normalized

    def validate_runtime_image(self, value):
        if value and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", value):
            raise serializers.ValidationError("Use a Docker image name or digest.")
        return value

    def validate_validation_commands(self, value):
        return validate_commands(value)

    def validate(self, attrs):
        commands = attrs.get("validation_commands", self.instance.validation_commands if self.instance else [])
        image = attrs.get("runtime_image", self.instance.runtime_image if self.instance else "")
        if commands and not image:
            raise serializers.ValidationError({"runtime_image": "Provide a prepared Docker image for custom checks, or leave commands empty for automatic detection."})
        return attrs


class SubmissionSerializer(serializers.Serializer):
    github_issue = serializers.CharField(max_length=700, required=False)
    # Kept for existing API clients and historical Jira investigations.
    jira_ticket = serializers.CharField(max_length=700, required=False)
    # Retain the live value for existing API clients; execution is always live.
    mode = serializers.ChoiceField(choices=["live"], default="live")
    repository = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_jira_ticket(self, value):
        try:
            return parse_ticket(value, settings.JIRA_BASE_URL)
        except ValueError as error:
            raise serializers.ValidationError(str(error)) from error

    def validate_repository(self, value):
        if not value.strip():
            return None
        query = Q(name__iexact=value.strip())
        if value.isdigit():
            query |= Q(pk=int(value))
        if value.startswith("https://"):
            try:
                query |= Q(url=normalize_repository_url(value))
            except ValueError as error:
                raise serializers.ValidationError(str(error)) from error
        matches = list(Repository.objects.filter(query, is_active=True)[:2])
        if not matches:
            raise serializers.ValidationError("Choose an active registered repository, or leave this blank for automatic discovery.")
        if len(matches) > 1:
            raise serializers.ValidationError("This repository name is ambiguous. Use its URL or ID.")
        return matches[0]

    def validate(self, attrs):
        repository = attrs.get("repository")
        if bool(attrs.get("github_issue")) == bool(attrs.get("jira_ticket")):
            raise serializers.ValidationError({"github_issue": "Provide a GitHub issue URL or issues/123 endpoint."})
        if attrs.get("github_issue"):
            try:
                attrs.update(parse_github_issue(attrs["github_issue"], repository.url if repository else ""))
            except ValueError as error:
                raise serializers.ValidationError({"github_issue": str(error)}) from error
            attrs["issue_source"] = "github"
        else:
            attrs.update(issue_source="jira", issue_key=attrs["jira_ticket"], issue_url=f"{settings.JIRA_BASE_URL}/browse/{attrs['jira_ticket']}" if settings.JIRA_BASE_URL else "")
        return attrs


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvestigationEvent
        fields = ["id", "stage", "message", "created_at"]


class InvestigationSerializer(serializers.ModelSerializer):
    repository = RepositorySerializer(read_only=True)
    requested_repository = serializers.SlugRelatedField(read_only=True, slug_field="name")

    class Meta:
        model = Investigation
        fields = ["id", "issue_source", "source_repository_url", "source_issue_number", "issue_key", "issue_url", "title", "requested_repository", "repository", "mode", "status", "stage",
                  "selection_reason", "branch", "base_branch", "publication_repository_url", "publication_head_owner", "pull_request_url", "issue_linked", "issue_comment_id", "issue_comment_url", "model_calls", "error", "attempt",
                  "created_at", "updated_at", "finished_at"]
        read_only_fields = fields


class InvestigationDetailSerializer(InvestigationSerializer):
    events = EventSerializer(many=True, read_only=True)
    coding_brief = serializers.SerializerMethodField()

    def get_coding_brief(self, obj):
        return coding_brief(obj.preprocessed) if obj.preprocessed else ""

    class Meta(InvestigationSerializer.Meta):
        fields = InvestigationSerializer.Meta.fields + ["ticket", "preprocessed", "coding_brief", "proposal", "validation", "events"]
        read_only_fields = fields
