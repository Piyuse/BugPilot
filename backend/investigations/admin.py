from django.contrib import admin
from .models import Investigation, InvestigationEvent, Repository

@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ["name", "url", "default_branch", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name", "description"]
    readonly_fields = ["default_branch"]


class EventInline(admin.TabularInline):
    model = InvestigationEvent
    extra = 0
    can_delete = False
    readonly_fields = ["stage", "message", "created_at"]


@admin.register(Investigation)
class InvestigationAdmin(admin.ModelAdmin):
    list_display = ["issue_key", "title", "mode", "status", "stage", "created_at"]
    list_filter = ["status", "mode"]
    search_fields = ["issue_key", "title"]
    readonly_fields = [field.name for field in Investigation._meta.fields]
    inlines = [EventInline]

    def has_add_permission(self, request):
        return False
