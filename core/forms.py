from django import forms
from django.contrib.auth.models import User
from .models import CompanyProfile, UserProfile
from django.core.exceptions import ValidationError


class OwnerUpdateForm(forms.ModelForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()

        if not email:
            raise ValidationError("Email is required.")

        # unique check excluding current user
        qs = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("This email is already in use.")

        return email


class CompanyUpdateForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ["name", "phone", "email", "address", "logo"]

class OwnerProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ["owner_ntn"]

    def clean_owner_ntn(self):
        ntn = (self.cleaned_data.get("owner_ntn") or "").strip()
        if not ntn:
            return ""  # optional
        allowed = set("0123456789-")
        if any(ch not in allowed for ch in ntn):
            raise ValidationError("NTN can contain only digits and hyphen (-).")
        return ntn