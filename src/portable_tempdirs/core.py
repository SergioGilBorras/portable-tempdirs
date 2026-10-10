"""Public API and lifecycle for portable temporary directories."""
from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from . import posix_backend, windows_backend
from .errors import TemporaryDirectoryError

_LOG = logging.getLogger("portable_tempdirs")

# Bounded retry is allowed ONLY for transient Windows sharing/locking statuses.
_WINDOWS_TRANSIENT_SHARING_CODES = {32, 33, 0xC0000043, 0xC0000054}
_WINDOWS_CLEANUP_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40)


def _is_transient_windows_cleanup_error(exc: TemporaryDirectoryError) -> bool:
    d = exc.diagnostic
    return (exc.code == "CLEANUP_INCOMPLETE" and isinstance(d, dict)
            and type(d.get("os_error_code")) is int
            and d["os_error_code"] in _WINDOWS_TRANSIENT_SHARING_CODES)


_NAMESPACE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("namespace must be a simple ASCII project identifier (1-48 characters)")
    if namespace.upper() in _WINDOWS_RESERVED or namespace.endswith((".", " ")):
        raise ValueError("namespace is reserved on Windows")
    return namespace


def _leaf_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(12)


class TemporaryDirectory:
    """Manage one exclusively created temporary directory.

    Construction creates the directory immediately and performs a write/read
    self-check. The context manager returns its path as ``str``. The owner has
    no finalizer; call :meth:`cleanup` or use it as a context manager.

    With ``parent=None`` the layout is ``system-temp/namespace/timestamp-random``.
    With an explicit parent the leaf is created directly beneath it, which is
    useful for same-parent atomic staging.

    Attributes:
        name: Created path as a string.
        path: Created path as a :class:`pathlib.Path`.
        privacy_assessment: Informational privacy status; it is not an
            attestation of effective host access.

    Use :meth:`detach` to retain a persistent workspace without leaving the
    parent's operating-system handle or descriptor open.

    Raises:
        ValueError: If the namespace or options are invalid.
        TemporaryDirectoryError: If creation or the I/O self-check fails.
    """

    def __init__(
        self,
        namespace: str,
        parent: os.PathLike[str] | str | None = None,
        collision_attempts: int = 8,
    ):
        """Create an owned directory immediately.

        Args:
            namespace: ASCII identifier used for the default namespace
                container. It must start with a letter and be 1-48 characters.
            parent: Optional absolute, existing parent directory.
            collision_attempts: Positive integer number of unique-name
                attempts; booleans are rejected.

        Raises:
            ValueError: If an argument fails validation.
            TypeError: If ``parent`` cannot be converted to a path.
            TemporaryDirectoryError: If creation or the I/O self-check fails.
        """
        self.namespace = _validate_namespace(namespace)
        if (
            isinstance(collision_attempts, bool)
            or not isinstance(collision_attempts, int)
            or collision_attempts < 1
        ):
            raise ValueError("collision_attempts must be a positive integer")
        self.collision_attempts = collision_attempts
        self.explicit_parent = Path(parent) if parent is not None else None
        if self.explicit_parent is not None and not self.explicit_parent.is_absolute():
            raise ValueError("parent must be absolute")
        self.path: Path | None = None
        self.name: str | None = None
        self.privacy_assessment: dict[str, str] = (
            {
                "status": "NO_VERIFICABLE",
                "scope": "Windows inherited ACL; this package does not audit effective access",
            }
            if os.name == "nt"
            else {
                "status": "MODE_0700",
                "scope": "POSIX directory mode checked; host-level access is not attested",
            }
        )
        self._backend: posix_backend.PosixDirectory | windows_backend.WindowsDirectory | None = None
        self._cleaned = False
        self._create()

    def _create(self) -> None:
        base = (self.explicit_parent if self.explicit_parent is not None
                else Path(tempfile.gettempdir()) / self.namespace)
        try:
            if os.name == "nt":
                self._backend = windows_backend.WindowsDirectory.create(
                    base,
                    self._leaf_name,
                    make_namespace=self.explicit_parent is None,
                    collision_attempts=self.collision_attempts,
                )
            else:
                self._backend = posix_backend.PosixDirectory.create(
                    base,
                    self._leaf_name,
                    make_namespace=self.explicit_parent is None,
                    collision_attempts=self.collision_attempts,
                )
            self.path = self._backend.path
            self.name = str(self.path)
            self._backend.verify_io()
        except TemporaryDirectoryError as exc:
            cleanup_diagnostic = self._cleanup_after_create_error()
            if cleanup_diagnostic:
                exc.diagnostic = {**exc.diagnostic, "creation_failure": exc.code,
                                  "cleanup": cleanup_diagnostic}
            raise
        except OSError as exc:
            cleanup_diagnostic = self._cleanup_after_create_error()
            error_code = "PERMISSION_DENIED" if isinstance(exc, PermissionError) else "CREATE_OR_SELF_CHECK_FAILED"
            raise TemporaryDirectoryError(
                f"temporary directory creation or self-check failed ({type(exc).__name__})",
                code=error_code,
                diagnostic={
                    "phase": "CREATE_OR_SELF_CHECK",
                    "category": type(exc).__name__,
                    "cleanup_status": (cleanup_diagnostic or {}).get("cleanup_status", "UNKNOWN"),
                    "directory_id": self._safe_id(),
                    **({"cleanup": cleanup_diagnostic} if cleanup_diagnostic else {}),
                },
            ) from None

    def _leaf_name(self) -> str:
        return _leaf_name()

    def _safe_id(self) -> str:
        return self.path.name if self.path is not None else "UNALLOCATED"

    def _cleanup_after_create_error(self) -> dict[str, object] | None:
        if self._backend is None:
            return None
        try:
            self._backend.cleanup()
            self._cleaned = True
            return None
        except TemporaryDirectoryError as exc:
            # The primary creation/self-check error remains the raised error;
            # cleanup diagnostics are also emitted by the backend logger.
            return exc.diagnostic

    def cleanup(self) -> None:
        """Attempt identity-checked cleanup; raise a structured error on failure.

        A failure can leave some or all of the tree behind. The owner remains
        retryable, but a later attempt is not guaranteed to succeed. After
        successful cleanup, subsequent calls are no-ops.
        """
        if self._cleaned or self._backend is None:
            return
        for attempt in range(len(_WINDOWS_CLEANUP_RETRY_DELAYS) + 1):
            try:
                self._backend.cleanup()
            except TemporaryDirectoryError as exc:
                if (os.name != "nt" or attempt == len(_WINDOWS_CLEANUP_RETRY_DELAYS)
                        or not _is_transient_windows_cleanup_error(exc)):
                    raise
                # The backend closes its handles before returning. Reentry
                # reopens and rechecks parent and root identities on each try.
                time.sleep(_WINDOWS_CLEANUP_RETRY_DELAYS[attempt])
            else:
                self._cleaned = True
                return

    def detach(self) -> Path:
        """Relinquish cleanup ownership while keeping the directory on disk.

        The caller assumes responsibility for the directory and its contents.
        This releases the backend's retained parent handle or descriptor; it
        does not delete, rename, or inspect the directory. It is not reversible,
        and the owner becomes inactive, just as after a successful transfer.
        """
        if self._cleaned or self._backend is None:
            raise TemporaryDirectoryError("temporary directory is not active", code="NOT_ACTIVE")
        self._backend.detach()
        self._cleaned = True
        assert self.path is not None
        return self.path

    def mark_moved_to(self, destination: os.PathLike[str] | str) -> None:
        """Transfer ownership after moving the directory to a sibling path.

        This method does not move the directory. The destination must be a
        sibling of the original path and must identify the same directory;
        the original name must no longer exist. Use a same-filesystem atomic
        rename before calling this method. Package-detected failures raise
        :class:`TemporaryDirectoryError`; an operating-system ``OSError`` may
        propagate during parent or destination lookup.
        """
        if self._cleaned or self._backend is None:
            raise TemporaryDirectoryError("temporary directory is not active", code="NOT_ACTIVE")
        self._backend.mark_moved_to(Path(destination))
        self._cleaned = True

    def __enter__(self) -> str:
        """Return the created directory path as a string while it is active.

        Raises:
            TemporaryDirectoryError: If cleanup or ownership transfer already
                made this owner inactive.
        """
        if self.name is None or self._cleaned:
            raise TemporaryDirectoryError("temporary directory is not active", code="NOT_ACTIVE")
        return self.name

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Attempt cleanup; preserve an active exception if cleanup also fails."""
        try:
            self.cleanup()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            diagnostic = getattr(cleanup_error, "diagnostic", None)
            if not isinstance(diagnostic, dict):
                diagnostic = {"phase": "CLEANUP", "category": type(cleanup_error).__name__,
                              "cleanup_status": "PENDING", "directory_id": self._safe_id()}
                _LOG.error("temporary cleanup deferred: %s", diagnostic)
            if hasattr(exc_value, "add_note"):
                exc_value.add_note("temporary cleanup also failed; see sanitized cleanup diagnostic")
            # Keep the primary exception and do not replace it with cleanup failure.
        return False


def temporary_directory(
    namespace: str,
    parent: os.PathLike[str] | str | None = None,
    *,
    collision_attempts: int = 8,
) -> TemporaryDirectory:
    """Create and validate a managed temporary directory immediately.

    Args:
        namespace: ASCII identifier used as the default-root namespace.
        parent: Optional absolute, existing parent directory. No fallback root
            is selected if creation beneath it fails.
        collision_attempts: Positive number of unique-name attempts. ``bool``
            is not accepted as an integer value.

    Returns:
        The owner of the new directory. Its context manager yields ``name``.

    Raises:
        ValueError: If an argument is invalid.
        TypeError: If ``parent`` cannot be converted to a path.
        TemporaryDirectoryError: If creation or its I/O self-check fails.
    """
    return TemporaryDirectory(namespace, parent, collision_attempts)
