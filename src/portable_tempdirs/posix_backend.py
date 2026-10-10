"""POSIX implementation using descriptor-relative creation and rmtree."""
from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

from .errors import TemporaryDirectoryError

_LOG = logging.getLogger("portable_tempdirs")
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class PosixDirectory:
    def __init__(self, parent_fd: int, parent_path: Path, parent_identity: tuple[int, int],
                 path: Path, leaf: str, identity: tuple[int, int]):
        self.parent_fd, self.parent_path, self.parent_identity = parent_fd, parent_path, parent_identity
        self.path, self.leaf, self.identity = path, leaf, identity
        self.cleaned = False

    @classmethod
    def create(cls, base: Path, leaf_factory, *, make_namespace: bool, collision_attempts: int):
        base = base.absolute()
        if make_namespace:
            # The system temp root is a trusted configured anchor. Create the
            # namespace relative to its open descriptor and reject an existing
            # non-private or symlink namespace.
            system_parent = base.parent
            parent_fd = os.open(system_parent, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
            try:
                try:
                    os.mkdir(base.name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                namespace_fd = os.open(base.name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                                       dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = namespace_fd
            except Exception:
                os.close(parent_fd)
                raise
            parent_path = base
            if stat.S_IMODE(os.fstat(parent_fd).st_mode) & 0o077:
                os.close(parent_fd)
                raise TemporaryDirectoryError("namespace directory is not private", code="PARENT_NOT_PRIVATE")
        else:
            # An explicit parent is selected by the caller. Its final component
            # is opened without following a symlink; ancestor trust is a caller
            # precondition documented by the API.
            parent_fd = os.open(base, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
            parent_path = base
        parent_stat = os.fstat(parent_fd)
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        leaf = None
        created = False
        try:
            for _ in range(collision_attempts):
                leaf = leaf_factory()
                try:
                    os.mkdir(leaf, 0o700, dir_fd=parent_fd)
                    created = True
                    break
                except FileExistsError:
                    continue
            else:
                raise TemporaryDirectoryError("unique temporary name allocation exhausted", code="COLLISION")
            child_fd = os.open(leaf, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(child_fd)
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise TemporaryDirectoryError("temporary directory is not private", code="MODE_UNSAFE")
                return cls(parent_fd, parent_path, parent_identity, base / leaf, leaf,
                           (info.st_dev, info.st_ino))
            finally:
                os.close(child_fd)
        except Exception as exc:
            if created and leaf is not None:
                diagnostic = {"phase": "CREATE", "category": type(exc).__name__,
                              "cleanup_status": "PENDING", "directory_id": leaf}
                _LOG.error("temporary cleanup deferred: %s", diagnostic)
            os.close(parent_fd)
            raise

    def verify_io(self):
        fd = os.open(self.leaf, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=self.parent_fd)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity:
                raise TemporaryDirectoryError("identity changed during self-check", code="IDENTITY_CHANGED")
            probe = ".portable-tempdirs-check"
            file_fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd)
            try:
                payload = b"portable-tempdirs"
                if os.write(file_fd, payload) != len(payload):
                    raise OSError("temporary short write")
            finally:
                os.close(file_fd)
            file_fd = os.open(probe, os.O_RDONLY | _O_NOFOLLOW, dir_fd=fd)
            try:
                if os.read(file_fd, 64) != b"portable-tempdirs":
                    raise OSError("temporary read-back mismatch")
            finally:
                os.close(file_fd)
            os.unlink(probe, dir_fd=fd)
        finally:
            os.close(fd)

    def cleanup(self):
        if self.cleaned:
            return
        try:
            if self.parent_fd is None:
                self.parent_fd = os.open(self.parent_path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
                info = os.fstat(self.parent_fd)
                if (info.st_dev, info.st_ino) != self.parent_identity:
                    os.close(self.parent_fd)
                    self.parent_fd = None
                    raise TemporaryDirectoryError("parent identity changed", code="IDENTITY_CHANGED")
            current = os.stat(self.leaf, dir_fd=self.parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != self.identity:
                raise TemporaryDirectoryError("temporary identity changed", code="IDENTITY_CHANGED")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise TemporaryDirectoryError("platform rmtree is not descriptor-safe", code="SAFE_RMTREE_UNAVAILABLE")
            shutil.rmtree(self.leaf, dir_fd=self.parent_fd)
            self.cleaned = True
        except Exception as exc:
            diagnostic = {"phase": "CLEANUP", "category": getattr(exc, "code", type(exc).__name__),
                          "cleanup_status": "PENDING", "directory_id": self.leaf}
            _LOG.error("temporary cleanup deferred: %s", diagnostic)
            if isinstance(exc, TemporaryDirectoryError):
                exc.diagnostic = diagnostic
                raise
            raise TemporaryDirectoryError("temporary cleanup incomplete", code="CLEANUP_INCOMPLETE",
                                          diagnostic=diagnostic) from None
        finally:
            if self.parent_fd is not None:
                os.close(self.parent_fd)
                self.parent_fd = None

    def detach(self):
        """Keep the tree and release the parent descriptor without deleting it."""
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None
        self.cleaned = True

    def mark_moved_to(self, destination: Path):
        destination = Path(destination)
        if destination.parent != self.path.parent:
            raise TemporaryDirectoryError("move destination is not a sibling", code="MOVE_OUTSIDE_PARENT")
        if self.parent_fd is None:
            self.parent_fd = os.open(self.parent_path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
            parent_info = os.fstat(self.parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != self.parent_identity:
                os.close(self.parent_fd)
                self.parent_fd = None
                raise TemporaryDirectoryError("parent identity changed", code="IDENTITY_CHANGED")
        info = os.stat(destination.name, dir_fd=self.parent_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != self.identity:
            raise TemporaryDirectoryError("moved directory identity mismatch", code="IDENTITY_CHANGED")
        try:
            os.stat(self.leaf, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            self.cleaned = True
            os.close(self.parent_fd)
            self.parent_fd = None
            return
        raise TemporaryDirectoryError("temporary source still exists after move", code="MOVE_NOT_COMPLETE")
