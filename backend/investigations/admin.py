from django.contrib import admin


from .models import BugReport,Repository


@admin.register(BugReport)
class BugReportAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "title",
        "repository_url",
        "created_at",
    ]

    search_fields = [
        "title",
        "description",
    ]

    readonly_fields = [
        "created_at",
    ]
    
@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "name",
        "url",
        "default_branch",
        "is_active",
    ]

    list_filter = [
        "is_active",
    ]

    search_fields = [
        "name",
        "url",
        "description",
    ]