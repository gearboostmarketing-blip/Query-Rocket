from django.urls import path

from . import views, views_connect, views_fixes, views_google

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("sites/<int:site_id>/", views.site_detail, name="site_detail"),
    path("sites/<int:site_id>/crawl/", views.site_crawl, name="site_crawl"),
    path("sites/<int:site_id>/status/", views.site_status, name="site_status"),
    path("sites/<int:site_id>/profile/", views.profile_edit, name="profile_edit"),
    path("sites/<int:site_id>/delete/", views.site_delete, name="site_delete"),
    path("sites/<int:site_id>/issues/", views.site_issues, name="site_issues"),
    path("pages/<int:page_id>/", views.page_detail, name="page_detail"),
    path("google/", views_google.google_home, name="google_home"),
    path("google/connect/", views_google.google_connect, name="google_connect"),
    path("google/callback/", views_google.google_callback, name="google_callback"),
    path("google/accounts/<int:account_id>/disconnect/", views_google.google_disconnect, name="google_disconnect"),
    path("sites/<int:site_id>/google/", views_google.site_google, name="site_google"),
    path("sites/<int:site_id>/google/sync/", views_google.site_google_sync, name="site_google_sync"),
    path("fixes/", views_fixes.all_fixes, name="all_fixes"),
    path("sites/<int:site_id>/fixes/", views_fixes.site_fixes, name="site_fixes"),
    path("sites/<int:site_id>/fixes/generate/", views_fixes.fixes_generate, name="fixes_generate"),
    path("sites/<int:site_id>/fixes/approve-all/", views_fixes.fixes_bulk_approve, name="fixes_bulk_approve"),
    path("fix/<int:fix_id>/", views_fixes.fix_action, name="fix_action"),
    path("sites/<int:site_id>/wordpress/", views_connect.site_connect, name="site_connect"),
    path("sites/<int:site_id>/autopilot/", views_connect.site_autopilot, name="site_autopilot"),
    path("sites/<int:site_id>/autopilot/run/", views_connect.autopilot_run_now, name="autopilot_run_now"),
    path("report/", views_connect.report_preview, name="report_preview"),
]
