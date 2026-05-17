from django.urls import path

from .views import (
    AllTasksView,
    DashboardView,
    MyWorkView,
    ProjectDetailView,
    ProjectListView,
    TaskDetailView,
    archive_task,
    create_task,
    post_comment,
    set_project_description,
    set_project_lead,
    set_task_assignee,
    set_task_description,
    set_task_due_date,
    set_task_priority,
    set_task_status,
    set_task_title,
    task_activity_fragment,
    task_comments_fragment,
    task_description_fragment,
    task_meta_fragment,
    task_title_fragment,
    task_topbar_title_fragment,
    toggle_project_member,
    toggle_task_label,
)

app_name = "web"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("my-work/", MyWorkView.as_view(), name="my_work"),
    path("tasks/", AllTasksView.as_view(), name="all_tasks"),
    path("tasks/new/", create_task, name="create_task"),
    path("projects/", ProjectListView.as_view(), name="project_list"),
    path("projects/<str:slug_prefix>/", ProjectDetailView.as_view(), name="project_detail"),
    path(
        "projects/<str:slug_prefix>/lead/",
        set_project_lead,
        name="set_project_lead",
    ),
    path(
        "projects/<str:slug_prefix>/description/",
        set_project_description,
        name="set_project_description",
    ),
    path(
        "projects/<str:slug_prefix>/members/toggle/",
        toggle_project_member,
        name="toggle_project_member",
    ),
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
        "projects/<str:slug_prefix>/<int:number>/title/",
        set_task_title,
        name="set_task_title",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/description/",
        set_task_description,
        name="set_task_description",
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
    path(
        "projects/<str:slug_prefix>/<int:number>/archive/",
        archive_task,
        name="archive_task",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/activity/",
        task_activity_fragment,
        name="task_activity_fragment",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/meta/",
        task_meta_fragment,
        name="task_meta_fragment",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/title-fragment/",
        task_title_fragment,
        name="task_title_fragment",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/topbar-title/",
        task_topbar_title_fragment,
        name="task_topbar_title_fragment",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/description-fragment/",
        task_description_fragment,
        name="task_description_fragment",
    ),
    path(
        "projects/<str:slug_prefix>/<int:number>/comments-fragment/",
        task_comments_fragment,
        name="task_comments_fragment",
    ),
]
