from rest_framework import serializers

from .models import BugReport,Repository

from .repository_urls import normalize_repository_url

class BugReportSerializer(serializers.ModelSerializer):
    class Meta:
        model=BugReport
        
        fields=[
            "id",
            "title",
            "description",
            "expected_behavior",
            "reproduction_steps",
            "error_details",
            "repository_url",
            "created_at",
        ]
        
        read_only_fields=[
            "id",
            "created_at"
        ]
        
        extra_kwargs={
            "title": {"max_length": 200},
            "description": {"max_length": 20000},
            "expected_behavior": {"max_length": 10000},
            "reproduction_steps": {"max_length": 10000},
            "error_details": {"max_length": 30000},
        }
        
class RepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model=Repository

        fields = [
            "id",
            "name",
            "url",
            "default_branch",
            "description",
            "is_active",
            "created_at",
        ]

        read_only_fields = [
            "id",
            "created_at",
        ]
        
    def validate_url(self,value):
        try:
            normalized_url=normalize_repository_url(value)
        except ValueError as error:
            raise serializers.ValidationError(str(error)) from error
        
        
        existing=Repository.objects.filter(url=normalized_url)
        
        if self.instance is not None:
            existing=existing.exclude(pk=self.instance.pk)
            
        if existing.exists():
            raise serializers.ValidationError(
                "This repository is already registered."
            )
        
        return normalized_url