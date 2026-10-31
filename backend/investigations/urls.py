from django.urls import path

from . import auth_views, views

urlpatterns = [
    path("auth/session/", auth_views.session_view, name="auth-session"),
    path("auth/login/", auth_views.login_view, name="auth-login"),
    path("auth/logout/", auth_views.logout_view, name="auth-logout"),
    path("health/", views.health_check, name="health-check"),
    path("overview/", views.overview, name="overview"),
    path("investigations/", views.InvestigationListView.as_view(), name="investigation-list"),
    path("investigations/<uuid:pk>/", views.InvestigationDetailView.as_view(), name="investigation-detail"),
    path("investigations/<uuid:pk>/retry/", views.retry_investigation, name="investigation-retry"),
    path("repositories/", views.RepositoryListCreateView.as_view(), name="repository-list-create"),
    path("repositories/<int:pk>/", views.RepositoryDetailView.as_view(), name="repository-detail"),
]
