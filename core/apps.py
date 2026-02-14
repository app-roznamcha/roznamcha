from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # ✅ Import signals only when apps are ready (prevents AppRegistryNotReady)
        try:
            import core.signals  # noqa: F401
        except Exception:
            # Keep startup safe; real errors will show when signals run
            pass