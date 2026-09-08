"""Public endpoint settings shared by evaluation Agent and Judge.

Credentials remain in the caller environment. Recorded settings can be checked
without consulting the machine's current configuration.
"""

from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from .canonical import sha256_digest

DEFAULT_API_BASE = "https://tokenhub.tencentmaas.com/v1"


@dataclass(frozen=True)
class ProviderEndpoint:
    api_base: str

    def __post_init__(self):
        base = self.api_base.rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("provider base must be credential-free HTTPS")
        object.__setattr__(self, "api_base", base)

    @property
    def origin(self):
        parsed = urlsplit(self.api_base)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def provider_id(self):
        return "tencent-tokenhub" if self.api_base == DEFAULT_API_BASE else "openai-compatible"

    @property
    def endpoint_id(self):
        return "configured-hy3-" + sha256_digest(self.api_base)[:20]

    @property
    def completions_url(self):
        return self.api_base + "/chat/completions"


def configured_endpoint():
    if os.environ.get("MODEL_NAME", "hy3") != "hy3":
        raise ValueError("this evaluation requires MODEL_NAME=hy3")
    return ProviderEndpoint(os.environ.get("OPENAI_API_BASE", DEFAULT_API_BASE))
