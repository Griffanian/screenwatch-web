from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/capture/", views.api_capture, name="api_capture"),
    path("api/capture/batch/", views.api_capture_batch, name="api_capture_batch"),
]
