"""CloudArc error types."""


class CloudArcError(Exception):
    """Base class for expected CloudArc failures."""


class FormatError(CloudArcError):
    """Archive or sidecar format is invalid."""


class SafetyError(CloudArcError):
    """An operation violates a safety policy."""


class CloudError(CloudArcError):
    """Cloud adapter failure."""


class CloudNotConfigured(CloudError):
    """A cloud provider is not configured in the current environment."""


class NativeBackendUnavailable(CloudArcError):
    """The optional native ViBo backend cannot run here."""
