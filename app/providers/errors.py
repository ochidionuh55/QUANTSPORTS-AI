"""Provider error model.

Every external failure is translated into one of these at the adapter
boundary. Business logic never sees an ``httpx.HTTPStatusError`` or a
provider's bespoke error envelope, because the moment it does, swapping the
provider becomes a change to every ``except`` block in the codebase.

The retryability of a failure is a property of the failure, not a decision for
the caller to guess at. ``is_retryable`` carries that judgement so that call
sites do not re-derive it — and so that authentication and data errors are
never retried, which would turn a misconfiguration into a rate-limit ban.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider failure.

    Attributes:
        provider_name: Which provider failed.
        operation: Which operation was attempted.
    """

    is_retryable: bool = False

    def __init__(
        self,
        message: str,
        provider_name: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_name = provider_name
        self.operation = operation

    def __str__(self) -> str:
        """Return the message prefixed with provider and operation context."""
        base = super().__str__()
        if self.provider_name and self.operation:
            return f"[{self.provider_name}:{self.operation}] {base}"
        if self.provider_name:
            return f"[{self.provider_name}] {base}"
        return base


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached, or returned a server error.

    Retryable: an outage is usually transient.
    """

    is_retryable = True


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not respond within the configured timeout."""

    is_retryable = True


class ProviderRateLimitError(ProviderError):
    """The provider rejected the call for exceeding its rate limit.

    Retryable only after backing off. Retrying immediately makes it worse,
    so ``retry_after_seconds`` carries the provider's own guidance when given.
    """

    is_retryable = True

    def __init__(
        self,
        message: str,
        provider_name: str | None = None,
        operation: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider_name, operation)
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthenticationError(ProviderError):
    """Credentials were missing, invalid or expired.

    Never retryable. Retrying a bad key produces nothing but a lockout.
    """

    is_retryable = False


class ProviderDataError(ProviderError):
    """The provider responded, but the payload could not be understood.

    Never retryable: an identical request returns the identical bad payload.
    Raised for schema drift, corrupt records and impossible values such as
    decimal odds at or below 1.0.
    """

    is_retryable = False


class ProviderCapabilityError(ProviderError):
    """The provider does not support the requested capability.

    Never retryable, and a programming error rather than an operational one:
    callers should ask ``supports()`` first.
    """

    is_retryable = False


class ProviderConfigurationError(ProviderError):
    """The provider is misconfigured and cannot be constructed."""

    is_retryable = False
