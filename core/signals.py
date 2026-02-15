# core/signals.py
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import seed_default_accounts_for_owner
from .models import UserProfile, CompanyProfile, Party

User = get_user_model()


@receiver(post_save, sender=User)
def ensure_userprofile_exists(sender, instance, created, **kwargs):
    """
    Auto-create UserProfile with correct default role:
    - Django superuser => SUPERADMIN
    - Everyone else => STAFF (until your signup sets OWNER explicitly)
    """
    profile, was_created = UserProfile.objects.get_or_create(user=instance)

    # If profile just created (or legacy wrong), fix role for Django superusers
    if instance.is_superuser and profile.role != "SUPERADMIN":
        profile.role = "SUPERADMIN"
        profile.owner = None
        profile.save(update_fields=["role", "owner"])


def _bootstrap_owner(owner_user):
    with transaction.atomic():
        CompanyProfile.objects.get_or_create(
            owner=owner_user,
            defaults={"name": owner_user.get_full_name() or owner_user.username},
        )
        seed_default_accounts_for_owner(owner_user)


@receiver(post_save, sender=UserProfile)
def ensure_company_and_accounts_for_owner(sender, instance, created, **kwargs):
    # Only OWNER gets bootstrap
    if getattr(instance, "role", None) != "OWNER":
        return
    _bootstrap_owner(instance.user)


@receiver(post_save, sender=Party)
def post_party_opening_balance(sender, instance, created, **kwargs):
    if created:
        instance.post_opening_balance()

