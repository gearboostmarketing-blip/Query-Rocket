from .models import Fix, Lesson, Site


def nav(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from .views_settings import setup_progress
    return {"nav_sites": Site.objects.only("id", "name"), "nav_waiting": Fix.objects.filter(status=Fix.PROPOSED).count(),
            "nav_setup": setup_progress(),
            "nav_lessons": Lesson.objects.filter(status=Lesson.SUGGESTED).count() if request.user.is_superuser else 0}
