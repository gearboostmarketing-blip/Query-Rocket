from django import forms

from .models import Site


class SiteForm(forms.ModelForm):
    class Meta:
        model = Site
        fields = ["name", "url", "max_pages"]
        labels = {"name": "Client name", "url": "Homepage URL", "max_pages": "Page limit"}
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Mountain View Farm & Garden"}),
            "url": forms.URLInput(attrs={"placeholder": "https://example.com/"}),
        }

    def clean_url(self):
        url = self.cleaned_data["url"].strip()
        if not url.endswith("/") and url.count("/") <= 2:
            url += "/"
        return url


def _lines(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


class ProfileForm(forms.Form):
    business_name = forms.CharField(required=False)
    business_type = forms.CharField(required=False, help_text="Plain words, e.g. auto repair shop")
    schema_type = forms.CharField(required=False, help_text="schema.org type, e.g. AutoRepair, Bakery, LocalBusiness")
    is_local_business = forms.BooleanField(required=False, label="Serves customers locally")
    phone = forms.CharField(required=False)
    email = forms.CharField(required=False)
    address = forms.CharField(required=False)
    hours = forms.CharField(required=False)
    services = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 5}), help_text="One per line")
    service_areas = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}), help_text="One per line")
    audience = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    brand_voice = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    LIST_FIELDS = ("services", "service_areas")

    @classmethod
    def from_profile(cls, profile):
        initial = dict(profile or {})
        for f in cls.LIST_FIELDS:
            initial[f] = "\n".join(initial.get(f) or [])
        return cls(initial=initial)

    def to_profile(self, existing):
        profile = dict(existing or {})
        for key, value in self.cleaned_data.items():
            profile[key] = _lines(value) if key in self.LIST_FIELDS else (value if value != "" else None)
        profile["is_local_business"] = self.cleaned_data["is_local_business"]
        return profile
