from rest_framework import serializers

from .models import BugReport

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