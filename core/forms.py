from django import forms
from django.contrib.auth.models import User
from .models import CompanyProfile

class OwnerUpdateForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email"]


class CompanyUpdateForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ["name", "phone", "email", "address", "logo"]