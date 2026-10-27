from django.db import models

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