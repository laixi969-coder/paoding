class PaodingError(Exception):
    code = "paoding_error"
    retryable = False


class ConfigurationError(PaodingError):
    code = "configuration_required"


class SourceError(PaodingError):
    code = "invalid_source"


class TransientError(PaodingError):
    code = "upstream_unavailable"
    retryable = True


class LeaseLost(PaodingError):
    code = "lease_lost"


class Conflict(PaodingError):
    code = "idempotency_conflict"
