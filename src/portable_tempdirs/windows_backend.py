"""Windows handle-relative directory creation and deletion.

The implementation uses documented Kernel32 handle APIs for enumeration,
identity inspection and disposition, and ``NtCreateFile`` only for opening a
child relative to a retained parent handle. It fails closed on unsupported
filesystems, reparse points, identity mismatches, sharing conflicts and races.
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
from ctypes import wintypes
from pathlib import Path

from .errors import TemporaryDirectoryError

_LOG = logging.getLogger("portable_tempdirs")
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILE_LIST_DIRECTORY = 0x0001
_FILE_ADD_SUBDIRECTORY = 0x0004
_FILE_READ_ATTRIBUTES = 0x0080
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_OBJ_CASE_INSENSITIVE = 0x40
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_DIRECTORY_FILE = 0x1
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_ID_BOTH_DIR_INFO = 10
_FILE_ID_BOTH_DIR_RESTART_INFO = 11
_FILE_DISPOSITION_INFO = 4
_ERROR_NO_MORE_FILES = 18
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPWSTR)]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(_UNICODE_STRING)), ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("CreationTimeLow", wintypes.DWORD),
                ("CreationTimeHigh", wintypes.DWORD), ("LastAccessTimeLow", wintypes.DWORD),
                ("LastAccessTimeHigh", wintypes.DWORD), ("LastWriteTimeLow", wintypes.DWORD),
                ("LastWriteTimeHigh", wintypes.DWORD), ("VolumeSerialNumber", wintypes.DWORD),
                ("FileSizeHigh", wintypes.DWORD), ("FileSizeLow", wintypes.DWORD),
                ("NumberOfLinks", wintypes.DWORD), ("FileIndexHigh", wintypes.DWORD),
                ("FileIndexLow", wintypes.DWORD)]


def _configure_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                    ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                   wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    kernel.SetFilePointerEx.restype = wintypes.BOOL
    ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                   ctypes.POINTER(_OBJECT_ATTRIBUTES), ctypes.POINTER(_IO_STATUS_BLOCK),
                                   ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
                                   wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtCreateFile.restype = ctypes.c_long
    return kernel, ntdll


def _status_code(status: int) -> int:
    return ctypes.c_uint32(status).value


def _identity(handle, kernel) -> tuple[int, int]:
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    return info.VolumeSerialNumber, (info.FileIndexHigh << 32) | info.FileIndexLow


def _attributes(handle, kernel) -> int:
    # FILE_ATTRIBUTE_TAG_INFO is two DWORDs; class 9.
    buffer = (wintypes.DWORD * 2)()
    if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(buffer), ctypes.sizeof(buffer)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandleEx failed")
    return int(buffer[0])


def _close(handle, kernel):
    if handle not in (None, 0, _INVALID_HANDLE):
        kernel.CloseHandle(handle)


def _nt_open(parent_handle, name: str, *, ntdll, access: int, share: int, disposition: int,
             directory: bool | None, create_options: int = 0):
    if not name or "\\" in name or "/" in name or name in {".", ".."}:
        raise TemporaryDirectoryError("unsafe child name", code="INVALID_CHILD_NAME")
    text = ctypes.create_unicode_buffer(name)
    # UNICODE_STRING.Length is a byte count of UTF-16 code units, not Python characters.
    name_size = len(name.encode("utf-16-le"))
    us = _UNICODE_STRING(name_size, name_size, ctypes.cast(text, wintypes.LPWSTR))
    obj = _OBJECT_ATTRIBUTES(ctypes.sizeof(_OBJECT_ATTRIBUTES), parent_handle, ctypes.pointer(us),
                             _OBJ_CASE_INSENSITIVE, None, None)
    iosb = _IO_STATUS_BLOCK()
    handle = wintypes.HANDLE()
    options = create_options | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
    if directory is True:
        options |= _FILE_DIRECTORY_FILE
    elif directory is False:
        options |= _FILE_NON_DIRECTORY_FILE
    status = ntdll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(obj), ctypes.byref(iosb),
                                None, _FILE_ATTRIBUTE_DIRECTORY if directory else 0, share, disposition,
                                options, None, 0)
    code = _status_code(status)
    if code & 0x80000000:
        if code == _STATUS_OBJECT_NAME_COLLISION:
            raise FileExistsError("exclusive temporary name collision")
        if code in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
            raise FileNotFoundError("handle-relative child not found")
        raise OSError(code, "handle-relative Windows open failed")
    return handle.value, int(iosb.Information)


def _open_path_directory(path: Path, kernel):
    handle = kernel.CreateFileW(str(path), _FILE_LIST_DIRECTORY | _FILE_ADD_SUBDIRECTORY |
                                _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                                _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING,
                                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == _INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "cannot open authorized parent directory")
    try:
        attrs = _attributes(handle, kernel)
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
            raise TemporaryDirectoryError("parent is not a plain directory", code="REPARSE_OR_NOT_DIRECTORY")
        return handle
    except Exception:
        _close(handle, kernel)
        raise


def _set_delete(handle, kernel):
    delete_flag = ctypes.c_ubyte(1)
    if not kernel.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO,
                                              ctypes.byref(delete_flag), ctypes.sizeof(delete_flag)):
        raise OSError(ctypes.get_last_error(), "handle disposition failed")


def _enumerate(handle, kernel) -> list[tuple[str, int, int]]:
    items: list[tuple[str, int, int]] = []
    buffer = ctypes.create_string_buffer(64 * 1024)
    info_class = _FILE_ID_BOTH_DIR_RESTART_INFO
    while True:
        ok = kernel.GetFileInformationByHandleEx(handle, info_class, buffer, len(buffer))
        info_class = _FILE_ID_BOTH_DIR_INFO
        if not ok:
            error = ctypes.get_last_error()
            if error == _ERROR_NO_MORE_FILES:
                break
            raise OSError(error, "handle-bound directory enumeration failed")
        offset = 0
        while True:
            (next_offset, _, _, _, _, _, _, _, attrs, name_length, _) = struct.unpack_from(
                "<IIqqqqqqIII", buffer, offset
            )
            file_id = struct.unpack_from("<q", buffer, offset + 96)[0]
            name = bytes(buffer[offset + 104:offset + 104 + name_length]).decode("utf-16-le", "strict")
            if name not in {".", ".."}:
                items.append((name, attrs, file_id & 0xFFFFFFFFFFFFFFFF))
            if not next_offset:
                break
            offset += next_offset
    return items


def _delete_tree(handle, expected_id: int, kernel, ntdll):
    attrs = _attributes(handle, kernel)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise TemporaryDirectoryError("refusing reparse point", code="REPARSE_POINT_REJECTED")
    if attrs & _FILE_ATTRIBUTE_DIRECTORY:
        for name, entry_attrs, enumerated_id in _enumerate(handle, kernel):
            is_dir = bool(entry_attrs & _FILE_ATTRIBUTE_DIRECTORY)
            child, _ = _nt_open(handle, name, ntdll=ntdll,
                                access=_DELETE | _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY | _SYNCHRONIZE,
                                share=_FILE_SHARE_READ, disposition=_FILE_OPEN, directory=is_dir)
            try:
                if _attributes(child, kernel) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise TemporaryDirectoryError("refusing reparse point", code="REPARSE_POINT_REJECTED")
                _, actual_id = _identity(child, kernel)
                if actual_id != enumerated_id:
                    raise TemporaryDirectoryError("child identity changed during cleanup", code="IDENTITY_CHANGED")
                _delete_tree(child, actual_id, kernel, ntdll)
            finally:
                _close(child, kernel)
    _, current_id = _identity(handle, kernel)
    if current_id != expected_id:
        raise TemporaryDirectoryError("identity changed before disposition", code="IDENTITY_CHANGED")
    _set_delete(handle, kernel)


class WindowsDirectory:
    def __init__(self, parent_handle, parent_path: Path, parent_identity: tuple[int, int],
                 path: Path, leaf: str, identity: tuple[int, int]):
        self.parent_handle, self.parent_path, self.parent_identity = parent_handle, parent_path, parent_identity
        self.path, self.leaf, self.identity = path, leaf, identity
        self.cleaned = False

    @classmethod
    def create(cls, base: Path, leaf_factory, *, make_namespace: bool, collision_attempts: int):
        if not base.is_absolute():
            raise TemporaryDirectoryError("parent must be absolute", code="INVALID_PARENT")
        kernel, ntdll = _configure_api()
        if base.name == "":
            raise TemporaryDirectoryError("filesystem root cannot be a project namespace", code="INVALID_PARENT")
        if make_namespace:
            # Default layout: ensure and open namespace relative to system temp.
            system_handle = _open_path_directory(base.parent, kernel)
            namespace_handle = None
            try:
                namespace_handle, _ = _nt_open(system_handle, base.name, ntdll=ntdll,
                    access=_FILE_LIST_DIRECTORY | _FILE_ADD_SUBDIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share=_FILE_SHARE_READ | _FILE_SHARE_WRITE, disposition=_FILE_OPEN_IF,
                    directory=True)
                attrs = _attributes(namespace_handle, kernel)
                if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise TemporaryDirectoryError("namespace is a reparse point", code="REPARSE_POINT_REJECTED")
                parent_handle = namespace_handle
                namespace_handle = None
            finally:
                _close(namespace_handle, kernel)
                _close(system_handle, kernel)
        else:
            parent_handle = _open_path_directory(base, kernel)
        try:
            parent_identity = _identity(parent_handle, kernel)
            child = None
            for _ in range(collision_attempts):
                leaf = leaf_factory()
                try:
                    child, _ = _nt_open(parent_handle, leaf, ntdll=ntdll,
                        access=_DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                        share=_FILE_SHARE_READ | _FILE_SHARE_WRITE, disposition=_FILE_CREATE,
                        directory=True)
                    break
                except FileExistsError:
                    continue
            else:
                raise TemporaryDirectoryError("unique temporary name allocation exhausted", code="COLLISION")
            attrs = _attributes(child, kernel)
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT or not attrs & _FILE_ATTRIBUTE_DIRECTORY:
                raise TemporaryDirectoryError("created object is not a plain directory", code="OBJECT_TYPE_INVALID")
            identity = _identity(child, kernel)
            result = cls(parent_handle, base, parent_identity, base / leaf, leaf, identity)
            _close(child, kernel)
            child = None
            return result
        except Exception:
            if child is not None:
                try:
                    # This is the handle returned directly by exclusive FILE_CREATE.
                    _set_delete(child, kernel)
                except OSError:
                    _LOG.error("temporary cleanup deferred: %s", {
                        "phase": "CREATE", "category": "PARTIAL_CREATE_CLEANUP_FAILED",
                        "cleanup_status": "PENDING", "directory_id": locals().get("leaf", "UNALLOCATED")})
                finally:
                    _close(child, kernel)
            _close(parent_handle, kernel)
            raise

    def verify_io(self):
        kernel, ntdll = _configure_api()
        root, _ = _nt_open(self.parent_handle, self.leaf, ntdll=ntdll,
                           access=_DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                           share=_FILE_SHARE_READ | _FILE_SHARE_WRITE, disposition=_FILE_OPEN, directory=True)
        try:
            if _identity(root, kernel) != self.identity:
                raise TemporaryDirectoryError("identity changed during self-check", code="IDENTITY_CHANGED")
            probe_name = ".portable-tempdirs-check"
            probe, _ = _nt_open(root, probe_name, ntdll=ntdll,
                                access=_DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE | 0x3,
                                share=_FILE_SHARE_READ, disposition=_FILE_CREATE, directory=False)
            try:
                payload = ctypes.create_string_buffer(b"portable-tempdirs")
                written = wintypes.DWORD()
                if not kernel.WriteFile(probe, payload, len(b"portable-tempdirs"), ctypes.byref(written), None):
                    raise OSError(ctypes.get_last_error(), "temporary write check failed")
                if written.value != len(b"portable-tempdirs"):
                    raise OSError("temporary short write")
                if not kernel.SetFilePointerEx(probe, 0, None, 0):
                    raise OSError(ctypes.get_last_error(), "temporary seek check failed")
                output = ctypes.create_string_buffer(len(b"portable-tempdirs"))
                read = wintypes.DWORD()
                if not kernel.ReadFile(probe, output, len(output), ctypes.byref(read), None):
                    raise OSError(ctypes.get_last_error(), "temporary read check failed")
                if output.raw != b"portable-tempdirs":
                    raise OSError("temporary read-back mismatch")
                _set_delete(probe, kernel)
            finally:
                _close(probe, kernel)
            try:
                unexpected, _ = _nt_open(root, probe_name, ntdll=ntdll,
                    access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                    share=_FILE_SHARE_READ | _FILE_SHARE_WRITE, disposition=_FILE_OPEN, directory=None)
            except FileNotFoundError:
                pass
            else:
                _close(unexpected, kernel)
                raise OSError("temporary self-check file remains after handle deletion")
        finally:
            _close(root, kernel)

    def cleanup(self):
        if self.cleaned:
            return
        kernel, ntdll = _configure_api()
        root = None
        try:
            if self.parent_handle is None:
                self.parent_handle = _open_path_directory(self.parent_path, kernel)
                if _identity(self.parent_handle, kernel) != self.parent_identity:
                    _close(self.parent_handle, kernel)
                    self.parent_handle = None
                    raise TemporaryDirectoryError("parent identity changed", code="IDENTITY_CHANGED")
            root, _ = _nt_open(self.parent_handle, self.leaf, ntdll=ntdll,
                               access=_DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                               share=_FILE_SHARE_READ, disposition=_FILE_OPEN, directory=True)
            if _identity(root, kernel) != self.identity:
                raise TemporaryDirectoryError("root identity changed", code="IDENTITY_CHANGED")
            _delete_tree(root, self.identity[1], kernel, ntdll)
            _close(root, kernel)
            root = None
            # Confirm by handle-relative open, not Path.exists().
            try:
                unexpected, _ = _nt_open(self.parent_handle, self.leaf, ntdll=ntdll,
                    access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE, share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    disposition=_FILE_OPEN, directory=None)
            except FileNotFoundError:
                self.cleaned = True
            else:
                _close(unexpected, kernel)
                raise TemporaryDirectoryError("directory name remains after disposition", code="DELETE_PENDING")
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
            if root is not None:
                _close(root, kernel)
            if self.parent_handle is not None:
                _close(self.parent_handle, kernel)
                self.parent_handle = None

    def mark_moved_to(self, destination: Path):
        if destination.parent != self.path.parent:
            raise TemporaryDirectoryError("move destination is not a sibling", code="MOVE_OUTSIDE_PARENT")
        kernel, ntdll = _configure_api()
        if self.parent_handle is None:
            self.parent_handle = _open_path_directory(self.parent_path, kernel)
            if _identity(self.parent_handle, kernel) != self.parent_identity:
                _close(self.parent_handle, kernel)
                self.parent_handle = None
                raise TemporaryDirectoryError("parent identity changed", code="IDENTITY_CHANGED")
        try:
            moved, _ = _nt_open(self.parent_handle, destination.name, ntdll=ntdll,
                access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE, share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                disposition=_FILE_OPEN, directory=True)
        except OSError:
            raise TemporaryDirectoryError("moved directory is unavailable", code="MOVE_NOT_FOUND") from None
        try:
            if _identity(moved, kernel) != self.identity:
                raise TemporaryDirectoryError("moved directory identity mismatch", code="IDENTITY_CHANGED")
            try:
                original, _ = _nt_open(self.parent_handle, self.leaf, ntdll=ntdll,
                    access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE, share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    disposition=_FILE_OPEN, directory=None)
            except FileNotFoundError:
                self.cleaned = True
                _close(self.parent_handle, kernel)
                self.parent_handle = None
            else:
                _close(original, kernel)
                raise TemporaryDirectoryError("temporary source still exists", code="MOVE_NOT_COMPLETE")
        finally:
            _close(moved, kernel)
