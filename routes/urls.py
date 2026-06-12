"""Routes app URL configuration."""

from django.urls import path
from . import views

urlpatterns = [
    path('route/', views.route_view, name='route'),
    path('health/', views.health_view, name='health'),
]
