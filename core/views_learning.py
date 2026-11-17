"""Super admin Learning page: lessons waiting for approval, lessons in use and whether they help, and reports."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai
from .learning import lesson_impact, report
from .models import AppSettings, Lesson

owner_required = user_passes_test(lambda u: u.is_active and u.is_superuser)


@login_required
@owner_required
def learning(request):
    lessons = Lesson.objects.select_related("site", "decided_user")
    active = list(lessons.filter(status=Lesson.ACTIVE))
    for l in active:
        l.impact = lesson_impact(l)
    rep = report()
    peak = max([m["total"] for m in rep["months"]] + [1])
    for m in rep["months"]:
        m["height"] = round(m["total"] / peak * 100)
    return render(request, "core/learning.html", {
        "suggested": lessons.filter(status=Lesson.SUGGESTED), "active": active,
        "dismissed": lessons.filter(status=Lesson.DISMISSED)[:30], "rep": rep, "app": AppSettings.load(),
        "ai_enabled": ai.ai_enabled(),
    })


@login_required
@owner_required
@require_POST
def lesson_action(request, lesson_id):
    lesson = get_object_or_404(Lesson, id=lesson_id)
    action = request.POST.get("action")
    text = request.POST.get("text", "").strip()
    if text and text != lesson.text:
        lesson.text, lesson.edited = text[:300], True
    if action in ("approve", "resume"):
        lesson.status = Lesson.ACTIVE
        msg = "In use: new fixes will follow it."
    elif action in ("dismiss", "turn_off"):
        lesson.status = Lesson.DISMISSED
        msg = "Turned off." if action == "turn_off" else "Dismissed. It won't be suggested again."
    elif action == "save":
        msg = "Saved."
    else:
        return redirect("learning")
    if action != "save":
        lesson.decided_at, lesson.decided_user = timezone.now(), request.user
    lesson.save()
    messages.success(request, msg)
    return redirect("learning")


@login_required
@owner_required
@require_POST
def lessons_find(request):
    from .tasks import find_lessons
    find_lessons.delay()
    messages.success(request, "Looking for new lessons in your decisions and results. Refresh in a minute.")
    return redirect("learning")
