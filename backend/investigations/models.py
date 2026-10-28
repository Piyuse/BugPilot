from django.db import models
from django.core.exceptions import ValidationError

from .repository_urls import normalize_repository_url

# Create your models here.
class BugReport(models.Model):
    title=models.CharField(max_length=200)
    description=models.TextField()
    expected_bahaviour=models.TextField(blank=True)
    reproduction_steps=models.TextField(blank=True)
    error_details=models.TextField(blank=True)
    
    repository_url=models.URLField(
        max_length=500,
        blank=True
    )
    
    created_at=models.DateField(auto_now_add=True)
    
    class Meta:
        ordering=["-created_at"]
        
    def __str__(self):
        return f"BUG-{self.pk}-{self.title}"
    
class Repository(models.Model):
    name=models.CharField(max_length=200)
    url=models.URLField(
        max_length=500,
        unique=True
    )
    default_branch=models.CharField(
        max_length=200,
        default="main"
    )
    description=models.CharField(blank=True)
    is_active=models.BooleanField(default=True)
    created_at=models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering=["name"]
        
    def clean(self):
        super().clean()
        
        try:
            self.url=normalize_repository_url(self.url)
        except ValueError as error:
            raise ValidationError(
                {"url":str(error)}
            )from error
            
    def __str__(self):
        return self.name
        
        