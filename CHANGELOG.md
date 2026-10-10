# Changelog

## 0.1.0 - 2026-10-10

- Initial standalone package release candidate.
- Added Windows handle-relative and POSIX descriptor-relative temporary directory backends.
- Added collision-safe creation, I/O self-checks, ownership-aware cleanup and structured errors.
- Added source distribution, wheel packaging, offline tests and cross-platform CI configuration.
- Added an explicit `detach()` lifecycle operation to retain an owned directory while releasing native parent handles/descriptors; `with` remains the default for disposable directories.

The library has passed its hosted Windows, Ubuntu, and macOS CPython 3.11–3.13 CI matrix. Version 0.1.0 is not yet published to PyPI or tagged as a GitHub release.
