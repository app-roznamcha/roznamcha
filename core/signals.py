# core/signals.py
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import seed_default_accounts_for_owner
from .models import UserProfile, CompanyProfile, Account, Party

User = get_user_model()


@receiver(post_save, sender=User)
def ensure_userprofile_exists(sender, instance, created, **kwargs):
    # Create on new users, but also safe for legacy users
    UserProfile.objects.get_or_create(user=instance)


def _bootstrap_owner(owner_user):
    """
    Ensures an OWNER has:
    - CompanyProfile
    - Default chart of accounts (via seed_default_accounts_for_owner)
    """
    with transaction.atomic():
        CompanyProfile.objects.get_or_create(
            owner=owner_user,
            defaults={"name": owner_user.get_full_name() or owner_user.username},
        )
        seed_default_accounts_for_owner(owner_user)


@receiver(post_save, sender=UserProfile)
def ensure_company_and_accounts_for_owner(sender, instance, created, **kwargs):
    profile = instance

    # Only OWNER gets bootstrap
    if getattr(profile, "role", None) != "OWNER":
        return

    _bootstrap_owner(profile.user)


@receiver(post_save, sender=Party)
def post_party_opening_balance(sender, instance, created, **kwargs):
    if created:
        instance.post_opening_balance()