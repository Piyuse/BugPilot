from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework import generics
from .models import BugReport
from .serializers import BugReportSerializer

# Create your views here.
@api_view(["GET"])
def health_check(request):
    return Response(
        {
            "application":"BugPilot",
            "status":"ok"
        }
    )
    
class BugReportPagination(PageNumberPagination):
    page_size=20
    
class BugReportListCreateView(generics.ListCreateAPIView):
    queryset=BugReport.objects.all()
    serializer_class=BugReportSerializer
    pagination_class=BugReportPagination
    
class BugReportDetailView(generics.RetrieveAPIView):
    queryset=BugReport.objects.all()
    serializer_class = BugReportSerializer
    