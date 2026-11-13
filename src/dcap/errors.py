class DcapError(Exception):
    """Base class for DCAP errors."""


class RegistryError(DcapError):
    """A definition is malformed or inconsistent with another registry."""


class ConflictError(DcapError):
    """A workspace change would overwrite something DCAP does not own."""


class ValidationFailure(DcapError):
    """Staged workspace changes failed validation."""


class StateError(DcapError):
    """The workspace path or DCAP's own state in it cannot be used."""
