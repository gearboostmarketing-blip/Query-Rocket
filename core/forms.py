from django import forms

from .models import Site


class SiteForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from . import conf
        self.fields["max_pages"].initial = conf.get("CRAWL_MAX_PAGES")

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


# ---------------------------------------------------------------- Settings page

class _Section(forms.Form):
    """Blank means 'use .env'. Secret fields are never shown back; blank keeps the saved one."""
    section = forms.CharField(widget=forms.HiddenInput)


class AISettingsForm(_Section):
    anthropic_key = forms.CharField(required=False, label="Anthropic API key",
                                    widget=forms.PasswordInput(attrs={"autocomplete": "off", "placeholder": "sk-ant-…"}))
    clear_anthropic_key = forms.BooleanField(required=False, label="Remove the saved key (go back to .env)")
    ai_model_profile = forms.CharField(required=False, label="Writing and review model")
    ai_model_classify = forms.CharField(required=False, label="Page sorting model")


class GoogleSettingsForm(_Section):
    google_client_id = forms.CharField(required=False, label="OAuth client ID")
    google_client_secret = forms.CharField(required=False, label="OAuth client secret",
                                           widget=forms.PasswordInput(attrs={"autocomplete": "off"}))


class EmailSettingsForm(_Section):
    report_email_to = forms.CharField(required=False, label="Weekly report goes to",
                                      help_text="Email addresses, separated by commas")
    email_from = forms.CharField(required=False, label="Sent from", help_text='e.g. Query Rocket <reports@gearboostmarketing.com>')
    smtp_host = forms.CharField(required=False, label="SMTP server", help_text="e.g. smtp.gmail.com")
    smtp_port = forms.IntegerField(required=False, min_value=1, max_value=65535, label="Port")
    smtp_user = forms.CharField(required=False, label="SMTP username")
    smtp_password = forms.CharField(required=False, label="SMTP password",
                                    widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                                    help_text="For Gmail, an app password (Google Account, Security, App passwords)")
    smtp_tls = forms.BooleanField(required=False, label="Use TLS (leave on for port 587)")

    def clean_report_email_to(self):
        from django.core.validators import validate_email
        raw = self.cleaned_data["report_email_to"]
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        for e in emails:
            try:
                validate_email(e)
            except forms.ValidationError:
                raise forms.ValidationError(f"{e} isn't an email address.")
        return ", ".join(emails)


class MapsSettingsForm(_Section):
    google_maps_key = forms.CharField(required=False, label="Google Maps API key",
                                      widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                                      help_text="Google Cloud, APIs & Services: enable Places API (New), then create an API key")


class EnginesSettingsForm(_Section):
    openai_key = forms.CharField(required=False, label="OpenAI API key (ChatGPT)", widget=forms.PasswordInput(attrs={"autocomplete": "off"}))
    perplexity_key = forms.CharField(required=False, label="Perplexity API key", widget=forms.PasswordInput(attrs={"autocomplete": "off"}))
    gemini_key = forms.CharField(required=False, label="Gemini API key", widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
                                 help_text="Google AI Studio. Claude uses your Anthropic key above.")
    openai_model = forms.CharField(required=False, label="ChatGPT model")
    perplexity_model = forms.CharField(required=False, label="Perplexity model")
    gemini_model = forms.CharField(required=False, label="Gemini model")
    ai_visibility_runs = forms.IntegerField(required=False, min_value=1, max_value=5, label="Times to ask each question",
                                            help_text="Answers vary; 2 or 3 gives a fair picture")


class CrawlSettingsForm(_Section):
    crawl_interval_days = forms.IntegerField(required=False, min_value=1, max_value=60, label="Recrawl every (days)")
    crawl_delay_seconds = forms.FloatField(required=False, min_value=0, max_value=10, label="Pause between pages (seconds)",
                                           help_text="Higher is gentler on client servers")
    default_max_pages = forms.IntegerField(required=False, min_value=10, max_value=20000, label="Page limit for new sites")
    fix_max_per_run = forms.IntegerField(required=False, min_value=1, max_value=500, label="Most new fixes per run",
                                         help_text="Caps AI spend per site per run")


class AddUserForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}), min_length=10,
                               help_text="At least 10 characters. They can change it on their Account page.")
    is_staff = forms.BooleanField(required=False, label="Can change settings and add people")

    def clean_username(self):
        from django.contrib.auth.models import User
        u = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=u).exists():
            raise forms.ValidationError("That username is taken.")
        return u


class AccountForm(forms.Form):
    first_name = forms.CharField(required=False, max_length=150)
    last_name = forms.CharField(required=False, max_length=150)
    email = forms.EmailField(required=False)
    weekly_report = forms.BooleanField(required=False, label="Email me the Monday report")
