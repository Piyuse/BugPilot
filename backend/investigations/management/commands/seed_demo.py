from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from investigations.models import Repository


class Command(BaseCommand):
    help = "Create local demo repositories and a demo login (demo / bugpilot-demo)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo seeding is only available with DEBUG=true.")
        user, created = get_user_model().objects.get_or_create(username="demo", defaults={"is_staff": True})
        if created:
            user.set_password("bugpilot-demo")
            user.save()
        for name, description in [
            ("checkout-service", "Payment orchestration, checkout totals, discounts and promotions. Python / Django."),
            ("storefront-web", "Customer storefront, product browsing and cart UI. TypeScript / React."),
            ("identity-service", "Authentication, user sessions, permissions and account management. Go."),
        ]:
            Repository.objects.get_or_create(name=name, defaults={
                "url": f"https://github.com/bugpilot-demo/{name}", "description": description,
                "validation_commands": [["python", "-m", "pytest"]],
            })
        self.stdout.write(self.style.SUCCESS("Demo repositories ready. Login: demo / bugpilot-demo" if created else "Demo repositories ready. Existing demo password was preserved."))
