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


class SourceChangedError(CloudArcError):
    """A source file changed while it was being read.

    Raised by the spooler, caught by the packer: by default such a file is
    skipped and reported (``reason: changed``) instead of aborting the whole
    archive, which is what a live log directory needs.
    """


class NativeBackendUnavailable(CloudArcError):
    """The optional native ViBo backend cannot run here."""
