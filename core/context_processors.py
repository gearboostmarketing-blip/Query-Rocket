from .models import Fix, Site


def nav(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"nav_sites": Site.objects.only("id", "name"), "nav_waiting": Fix.objects.filter(status=Fix.PROPOSED).count()}
