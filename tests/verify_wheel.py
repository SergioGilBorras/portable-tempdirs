"""Build and verify an isolated installation of a wheel from a distribution directory."""
from __future__ import annotations

import ast
import email.parser
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile
from email.policy import default
from pathlib import Path


class WheelVerificationError(RuntimeError):
    """Raised when a wheel is stale, incomplete, or has unexpected metadata."""


def _contains_detach(core_source: bytes) -> bool:
    try:
        module = ast.parse(core_source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == "TemporaryDirectory"
        and any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == "detach"
            for member in node.body
        )
        for node in module.body
    )


def validate_wheel_archive(
    wheel: Path, *, project_name: str, expected_version: str
) -> email.message.Message:
    """Check wheel metadata and reject archives without the public detach method."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_paths = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise WheelVerificationError(
                    "wheel must contain exactly one .dist-info/METADATA file"
                )
            metadata = email.parser.BytesParser(policy=default).parsebytes(
                archive.read(metadata_paths[0])
            )
            canonical_name = lambda value: value.lower().replace("_", "-").replace(".", "-")
            if canonical_name(metadata.get("Name", "")) != canonical_name(project_name):
                raise WheelVerificationError(
                    f"wheel project name does not match {project_name!r}"
                )
            if metadata.get("Version") != expected_version:
                raise WheelVerificationError(
                    f"wheel version {metadata.get('Version')!r} does not match "
                    f"pyproject.toml version {expected_version!r}"
                )
            if metadata.get_all("Requires-Dist"):
                raise WheelVerificationError("wheel unexpectedly declares runtime dependencies")
            if "portable_tempdirs/__init__.py" not in names:
                raise WheelVerificationError("wheel is missing portable_tempdirs/__init__.py")
            if "portable_tempdirs/py.typed" not in names:
                raise WheelVerificationError("wheel is missing portable_tempdirs/py.typed")
            if "portable_tempdirs/core.py" not in names:
                raise WheelVerificationError("wheel is missing portable_tempdirs/core.py")
            if not _contains_detach(archive.read("portable_tempdirs/core.py")):
                raise WheelVerificationError(
                    "wheel is stale: TemporaryDirectory.detach() is missing"
                )
            return metadata
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise WheelVerificationError(
            f"cannot inspect wheel archive ({type(exc).__name__})"
        ) from None


def _installed_check_code() -> str:
    return """
import importlib.metadata as metadata
import pathlib
import shutil
import sys
from portable_tempdirs import TemporaryDirectory, temporary_directory
from pathlib import Path

expected_version, install_root, checkout_root, workspace_root = sys.argv[1:]
install_root = Path(install_root).resolve()
checkout_root = Path(checkout_root).resolve()
module_path = Path(__import__('portable_tempdirs').__file__).resolve()
assert module_path.is_relative_to(install_root), (module_path, install_root)
assert checkout_root not in module_path.parents, module_path
distribution = metadata.distribution('portable-tempdirs')
assert distribution.version == expected_version == metadata.version('portable-tempdirs')
assert not distribution.requires
assert callable(getattr(TemporaryDirectory, 'detach', None)), 'TemporaryDirectory.detach() is missing'

workspace = Path(workspace_root) / 'wheel-functional-check'
workspace.mkdir()
try:
    disposable = temporary_directory('WheelCleanup', parent=workspace)
    disposable_path = disposable.path
    probe = disposable_path / 'probe.txt'
    probe.write_text('wheel-install-ok', encoding='utf-8')
    assert probe.read_text(encoding='utf-8') == 'wheel-install-ok'
    disposable.cleanup()
    assert not disposable_path.exists(), 'cleanup() did not remove the disposable directory'

    owner = temporary_directory('WheelDetach', parent=workspace)
    persistent_path = owner.path
    marker = persistent_path / 'persistent.txt'
    marker.write_text('retained-after-detach', encoding='utf-8')
    backend = owner._backend
    resource_name = next(
        (name for name in ('parent_handle', 'parent_fd') if hasattr(backend, name)), None
    )
    assert resource_name is not None, 'backend has no inspectable owned parent resource'
    assert getattr(backend, resource_name) is not None, 'parent resource was not held before detach'
    detached_path = owner.detach()
    assert detached_path == persistent_path
    assert getattr(backend, resource_name) is None, 'detach() did not release the parent resource'
    owner.cleanup()
    assert persistent_path.is_dir(), 'cleanup() removed a detached directory'
    assert marker.read_text(encoding='utf-8') == 'retained-after-detach'
finally:
    # This unique workspace belongs exclusively to this verifier invocation.
    shutil.rmtree(workspace)
print('installed wheel import, cleanup, I/O, detach and resource release: OK')
"""


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_wheel.py DIST_DIRECTORY")
    checkout = Path(__file__).resolve().parents[1]
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    project_name = project["name"]
    project_version = project["version"]
    dist = Path(sys.argv[1]).resolve()
    wheels = sorted(dist.glob("portable_tempdirs-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one portable_tempdirs wheel in {dist}")
    wheel = wheels[0]
    expected_prefix = f"portable_tempdirs-{project_version}-"
    if not wheel.name.startswith(expected_prefix):
        raise SystemExit(f"wheel filename does not match project version {project_version}")
    validate_wheel_archive(
        wheel, project_name=project_name, expected_version=project_version
    )

    root = Path(tempfile.mkdtemp(prefix="portable-tempdirs-wheel-"))
    try:
        prefix = root / "install-prefix"
        purelib = Path(
            sysconfig.get_path("purelib", vars={"base": str(prefix), "platbase": str(prefix)})
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        run_command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--no-cache-dir",
            "--ignore-installed",
            "--prefix",
            str(prefix),
            str(wheel),
        ]
        subprocess.run(run_command, cwd=root, env=environment, check=True, timeout=120)
        code = _installed_check_code()
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                f"import sys; sys.path.insert(0, {str(purelib)!r}); exec({code!r})",
                project_version,
                str(purelib),
                str(checkout),
                str(root),
            ],
            cwd=root,
            env=environment,
            check=True,
            timeout=60,
        )
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            # Do not mask a verification failure or weaken filesystem policy.
            print(
                f"wheel verification workspace retained after cleanup denial: {type(exc).__name__}",
                file=sys.stderr,
            )
    print(f"isolated wheel verification: {project_name} {project_version}: OK")


if __name__ == "__main__":
    main()
