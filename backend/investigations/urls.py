from django.urls import path

from .views import health_check,BugReportListCreateView,BugReportDetailView,RepositoryDetailView,RepositoryListCreateView


urlpatterns = [
    path("health/", health_check, name="health-check"),
        path(
        "bug-reports/",
        BugReportListCreateView.as_view(),
        name="bug-report-list-create",
    ),
    path(
        "bug-reports/<int:pk>/",
        BugReportDetailView.as_view(),
        name="bug-report-detail",
    ),
    path(
    "repositories/",
    RepositoryListCreateView.as_view(),
    name="repository-list-create",
),
path(
    "repositories/<int:pk>/",
    RepositoryDetailView.as_view(),
    name="repository-detail",
),
]