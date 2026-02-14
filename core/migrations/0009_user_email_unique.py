from django.db import migrations


def forwards(apps, schema_editor):
    User = apps.get_model("auth", "User")

    # 1) Normalize emails + fix blanks
    seen = set()

    for u in User.objects.all().only("id", "username", "email"):
        email = (u.email or "").strip().lower()

        # If blank -> give deterministic unique dummy email
        if not email:
            email = f"noemail+u{u.id}@example.invalid"

        # If duplicate after lowering -> make unique deterministically
        if email in seen:
            # keep domain if possible
            if "@" in email:
                local, domain = email.split("@", 1)
                email = f"{local}+dup{u.id}@{domain}"
            else:
                email = f"dup+u{u.id}@example.invalid"

        seen.add(email)

        if u.email != email:
            u.email = email
            u.save(update_fields=["email"])

    # 2) Add unique constraint at DB level
    # Note: Django's auth_user table name is stable for default User.
    schema_editor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_auth_user_email ON auth_user(email);"
    )


def backwards(apps, schema_editor):
    # Remove the index on rollback
    schema_editor.execute("DROP INDEX IF EXISTS uniq_auth_user_email;")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_alter_companyprofile_slug"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]