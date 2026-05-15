from django.urls import path

from .views import DashboardView, ProjectDetailView, ProjectListView, TaskDetailView

app_name = "web"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("projects/", ProjectListView.as_view(), name="project_list"),
    path("projects/<str:slug_prefix>/", ProjectDetailView.as_view(), name="project_detail"),
    path(
        "projects/<str:slug_prefix>/<int:number>/",
        TaskDetailView.as_view(),
        name="task_detail",
    ),
]
