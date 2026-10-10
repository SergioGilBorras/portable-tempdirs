# portable-tempdirs

`portable-tempdirs` is a small, standard-library-only Python package for creating and managing temporary directory trees. It creates a directory immediately, checks that it can write and read a small probe file, and provides conservative cleanup tied to the identity of the created directory.

## Install

Once published, install it with:

```console
python -m pip install portable-tempdirs
```

To install a checkout locally:

```console
python -m pip install .
```

The package metadata declares version `0.1.0` and Python `>=3.11`. The GitHub Actions matrix is configured for CPython 3.11, 3.12 and 3.13 on Windows, Ubuntu Linux and macOS. The full matrix must pass before claiming support across every listed combination.

## Quick start

```python
from pathlib import Path

from portable_tempdirs import temporary_directory

with temporary_directory("my-app") as temp_path:
    workspace = Path(temp_path)
    input_file = workspace / "input.txt"
    input_file.write_text("example", encoding="utf-8")
    assert input_file.read_text(encoding="utf-8") == "example"
```

The context manager yields the directory path as a `str` and attempts cleanup on exit. If cleanup fails with no other exception propagating, the cleanup error is raised. If the body is already raising, that original exception remains primary and receives a note about the cleanup failure.

## Public API

The package exports `TemporaryDirectory`, `TemporaryDirectoryError` and `temporary_directory`.

### `temporary_directory(namespace, parent=None, *, collision_attempts=8)`

Creates and validates a managed directory immediately, then returns its `TemporaryDirectory` owner.

- `namespace` must be an ASCII identifier of 1-48 characters. It starts with a letter and contains only letters, digits, `_` or `-`. The Windows device names `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9` and `LPT1`-`LPT9` are rejected.
- With `parent=None`, the layout is `<tempfile.gettempdir()>/<namespace>/<UTC timestamp>-<random token>`.
- An explicit `parent` must be absolute, already exist and be a directory. The new directory is placed directly below it. The caller is responsible for choosing a parent and ancestor chain it is authorized to use and trust.
- `collision_attempts` is a positive integer, excluding `bool`. Name allocation stops with a structured error after this many collisions; another root is never tried as a fallback.

The returned owner exposes:

- `name: str` and `path: pathlib.Path`, the created directory path.
- `cleanup()`, which attempts identity-checked cleanup and is a no-op after successful cleanup. It may be retried after a reported failure, without weakening identity checks.
- `detach()`, which releases the owner's handle/descriptor and retains the directory in place. The caller assumes permanent cleanup responsibility; the owner becomes inactive and a later `cleanup()` is a no-op. Use this for persistent workspaces without leaving handles open.
- `mark_moved_to(destination)`, which transfers cleanup responsibility after the caller has moved the directory to a sibling path on the same filesystem. This method does not perform the move.
- `privacy_assessment`, an informational mapping. POSIX reports the literal status `MODE_0700` after checking the directory mode. Windows reports the literal status `NO_VERIFICABLE` (not verifiable), since ACL inheritance is not audited. Neither value attests to host-level effective access.

Use `temporary_directory("my-app").detach()` when a directory must survive after the owning object is discarded. Do not call `detach()` inside a `with` block expecting automatic cleanup; detached content remains for application-managed retention.

The owner has no finalizer. Directories are not automatically removed when the process exits or when an owner is garbage-collected. Use the context manager or call `cleanup()` explicitly. A cleanup error means the tree may remain; inspect the sanitized diagnostic and confirm ownership before manual removal. The library never sweeps old directories.

The namespace container under the system temporary root may remain after its last managed child is cleaned up. It is not a managed workspace and is not automatically removed. Applications that use a fixed namespace should account for this persistent container.

### Errors

Validation failures raise `ValueError`; a value that cannot be converted to a path can raise `TypeError`. Most package-detected operational failures raise `TemporaryDirectoryError`, an `OSError` subclass with a machine-readable `code` and a `diagnostic` mapping. `mark_moved_to()` can also propagate an underlying `OSError` if the operating system rejects a parent or destination lookup. Package-generated diagnostics and log records omit absolute paths and include categories, phases and an opaque directory identifier where available; arbitrary diagnostics supplied by callers when constructing the public exception are not sanitized. Error codes are intended to remain stable; diagnostic fields may grow.

Current `TemporaryDirectoryError.code` values are `CLEANUP_INCOMPLETE`, `COLLISION`, `CREATE_OR_SELF_CHECK_FAILED`, `DELETE_PENDING`, `IDENTITY_CHANGED`, `INVALID_CHILD_NAME`, `INVALID_PARENT`, `MODE_UNSAFE`, `MOVE_NOT_COMPLETE`, `MOVE_NOT_FOUND`, `MOVE_OUTSIDE_PARENT`, `NOT_ACTIVE`, `OBJECT_TYPE_INVALID`, `PARENT_NOT_PRIVATE`, `PERMISSION_DENIED`, `REPARSE_OR_NOT_DIRECTORY`, `REPARSE_POINT_REJECTED` and `SAFE_RMTREE_UNAVAILABLE`. Some codes are specific to one backend or operation. A failed cleanup is logged with `cleanup_status: "PENDING"` and may be retried, but the tree may have been partly removed and a retry is not guaranteed to succeed.

## Security and filesystem behavior

- Names are created exclusively and include a cryptographically random component. Collisions are retried a bounded number of times; existing entries are not reused or deleted.
- Both backends perform a write/read self-check before returning. Failure is reported; the package does not silently select another temporary root.
- POSIX creation uses directory-relative operations, `O_NOFOLLOW`/`O_DIRECTORY` where available, mode `0700`, and inode/device identity checks. Cleanup requires a `shutil.rmtree` implementation that advertises symlink-attack resistance. Symlinks inside the tree are unlinked as entries; their targets are not traversed.
- Windows uses exclusive handle-relative creation, handle-bound enumeration and identity checks. Cleanup rejects reparse points, identity changes and sharing/access failures. It does not fall back to path-based recursive deletion.
- The Windows backend uses the native `NtCreateFile` entry point from `ntdll` for relative opens. This is not a documented stable Win32 API. Filesystems and Windows versions not exercised by CI may reject operations; the backend fails closed in that case.
- Explicit parents are a trust boundary. The final parent component is opened without following a link on Windows and on POSIX systems that provide `O_NOFOLLOW`. On POSIX systems without that flag, the backend cannot provide that final-component no-follow guarantee. Ancestors are not independently secured on either platform. Do not use a parent that an untrusted process can replace or modify concurrently. In particular, POSIX `mkdir` returns no directory handle; the implementation opens the new random name immediately afterward. A hostile process able to race changes in a shared writable parent is outside the guarantee.
- The package does not change ACLs, permissions outside the new directory, environment variables or system configuration. It does not elevate privileges or bypass sandbox restrictions. POSIX mode `0700` is not a claim about all host-level access controls; Windows inherits parent ACLs and does not report effective privacy.
- Same-identity hostile processes can modify files in a workspace while it is in use. Identity checks reduce name-substitution risks during cleanup but do not make an actively shared workspace safe from its own user or host.

## Compatibility

The intended targets are Windows and POSIX systems, including Linux and macOS, with CPython 3.11-3.13. Local validation is platform-dependent; see the CI workflow and release notes for combinations actually exercised. The POSIX backend requires descriptor-relative filesystem operations and a symlink-resistant `shutil.rmtree`. Windows behavior depends on native API and filesystem support. No guarantee is made for network filesystems, unusual filesystems, or Windows releases not covered by CI.

There are no runtime dependencies. Building from source uses the `setuptools` build backend declared in `pyproject.toml`.

For version history, see [CHANGELOG.md](CHANGELOG.md). Contributor setup and security-sensitive review expectations are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Development and contribution

Run the offline unit/integration suite:

```console
python -m unittest discover -s tests -v
```

Build the wheel and source distribution, then install the wheel into an isolated target and exercise the documented API:

```console
python -m pip install build
python -m build
python tests/verify_wheel.py dist
```

The wheel verifier needs pip but no network access because installation uses the locally built wheel with `--no-deps`. Platform-specific tests skip when run on another operating system; synthetic-link tests can also skip when host policy prevents creating their fixture. Skip reasons are explicit, and tests do not elevate privileges. Before submitting a change, run the suite on the affected platform, build both archives, inspect their contents, and confirm the GitHub Actions matrix passes. Do not publish a release or upload to PyPI as part of ordinary development.
