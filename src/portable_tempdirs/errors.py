"""Public errors shared by the platform backends."""


class TemporaryDirectoryError(OSError):
    """Operational error with a machine-readable code and diagnostic mapping.

    Diagnostics created by this package omit absolute paths. Callers that pass
    their own ``diagnostic`` mapping are responsible for sanitizing its values.
    """

    def __init__(self, message: str, *, code: str, diagnostic: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic or {"phase": "UNKNOWN", "category": code}
