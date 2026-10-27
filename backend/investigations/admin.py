from django.contrib import admin


from .models import BugReport


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