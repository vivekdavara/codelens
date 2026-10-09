"""Model providers: recorded (the default) and opt-in live vendors. See DESIGN.md, "Providers"."""

from codelens.providers.base import (
    Completion,
    Provider,
    ProviderError,
    ProviderHTTPError,
    ProviderRefused,
    ProviderTruncated,
    RecordingMissing,
    Request,
    Usage,
)
from codelens.providers.recorded import RecordedProvider, Recorder

__all__ = [
    "Completion",
    "Provider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRefused",
    "ProviderTruncated",
    "RecordedProvider",
    "Recorder",
    "RecordingMissing",
    "Request",
    "Usage",
]
