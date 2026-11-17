from django.core.management.base import BaseCommand, CommandError

from core.models import CrawlRun, Site
from core.tasks import crawl_site


class Command(BaseCommand):
    help = "Crawl a site right now in this terminal (no queue). Usage: manage.py crawl_site <site id>"

    def add_arguments(self, parser):
        parser.add_argument("site_id", type=int)

    def handle(self, site_id, **opts):
        try:
            site = Site.objects.get(id=site_id)
        except Site.DoesNotExist:
            raise CommandError(f"No site with id {site_id}")
        run = CrawlRun.objects.create(site=site, stage="Starting")
        crawl_site.apply(args=[run.id])
        run.refresh_from_db()
        self.stdout.write(f"{run.status}: {run.pages_crawled} pages, {run.issues_found} issues. {run.error}")
