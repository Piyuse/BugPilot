from django.contrib import admin
from django.urls import include, path

from investigations.auth_views import backend_info

urlpatterns = [
    path("", backend_info, name="backend-info"),
    path("admin/", admin.site.urls),
    path("api/", include("investigations.urls")),
]
