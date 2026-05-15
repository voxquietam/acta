from django.urls import path

from .views import (
    DashboardView,
    ProjectDetailView,
    ProjectListView,
    TaskDetailView,
    post_comment,
    set_task_assignee,
    set_task_due_date,
    set_task_priority,
    set_task_status,
    toggle_task_label,
)

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
    path(
        "projects/<str:slug_prefix>/<int:number>/status/",
        set_task_status,
        name="set_task_status",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/priority/",
        set_task_priority,
        name="set_task_priority",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/assignee/",
        set_task_assignee,
        name="set_task_assignee",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/due-date/",
        set_task_due_date,
        name="set_task_due_date",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/labels/toggle/",
        toggle_task_label,
        name="toggle_task_label",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/comments/",
        post_comment,
        name="post_comment",
    ),
]
