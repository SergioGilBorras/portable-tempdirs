# Contributing

## Requirements

- CPython 3.11 or later.
- No runtime dependencies are required.
- A build environment needs the `setuptools` backend declared in `pyproject.toml` and the `build` frontend for the documented archive command.

## Local checks

Create a virtual environment, then install the build frontend in it. Do not install development tools into a global Python installation:

```console
python -m venv .venv
```

Use `\.venv\Scripts\python.exe` on Windows PowerShell or `.venv/bin/python` on Linux and macOS. The commands below use the POSIX path:

```console
.venv/bin/python -m pip install build
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m build
.venv/bin/python tests/verify_wheel.py dist
```

The tests use `unittest`; pytest is not a runtime or development dependency. Tests for another operating system are skipped on the current platform. Synthetic-link tests may also skip when host policy prevents creating their fixture; each skip reports its reason. Do not elevate privileges, alter ACLs, or weaken sandbox settings to make such a test run.

Before submitting changes, inspect both `dist/portable_tempdirs-*.whl` and `dist/portable_tempdirs-*.tar.gz`. The wheel should contain only the importable package, typing marker and distribution metadata. The source archive should contain the package, tests and maintained project documentation. Never commit build output, caches, credentials, local editor/agent settings, or temporary test workspaces.

## Security-sensitive changes

Preserve exclusive creation, identity checks, no-follow behavior, conservative failure handling and the documented threat boundary. Add regression tests for any change to creation, cleanup, handle/descriptor lifetime, native calls or path validation. Do not add path-based fallback cleanup, global permission changes or automatic sweeps of old workspaces.

## Pull requests and releases

Changes should pass the complete GitHub Actions matrix on Windows, Ubuntu Linux and macOS for CPython 3.11-3.13. State any locally unavailable platform/version checks in the pull request; a locally blocked wheel-install test is not evidence that the hosted check passed. Do not publish to PyPI or create a release/tag without separate maintainer authorization and successful CI.
