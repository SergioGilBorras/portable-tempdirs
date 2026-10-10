# Changelog

## 0.1.2 - 2026-10-10

- Include sanitized numeric Windows OS error codes in managed cleanup diagnostics, never paths or exception text.
- Retry only transient Windows sharing/file-lock failures with bounded delays and identity-checked reopening; persistently locked resources and all other failures still raise.
- Add regression checks for diagnostic codes and bounded retry.

## 0.1.1 - 2026-10-10

- Bumped the package version after `TemporaryDirectory.detach()` was added without changing the `0.1.0` version, which allowed wheels with different APIs to use the same version identifier. The consumer-reported older wheel could not be inspected, so its exact provenance remains unknown.
- Strengthened the wheel verifier to compare project and archive versions, inspect the installed import location and dependencies, and exercise cleanup, I/O, detach, persistence, and native resource release.
- Added a regression test proving that the wheel verifier rejects an archive without `TemporaryDirectory.detach()`.
- Corrected documentation and examples to describe the owner-based `detach()` API and virtual-environment-based development commands.

## 0.1.0 - 2026-10-10

- Initial standalone package release candidate.
- Added Windows handle-relative and POSIX descriptor-relative temporary directory backends.
- Added collision-safe creation, I/O self-checks, ownership-aware cleanup and structured errors.
- Added source distribution, wheel packaging, offline tests and cross-platform CI configuration.
- The `detach()` lifecycle operation was added to the 0.1.0 code line without a version increment; 0.1.1 corrects the package version so wheels with different APIs are distinguishable.
