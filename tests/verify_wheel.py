"""Build and verify an isolated installation of a wheel from a distribution directory."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_wheel.py DIST_DIRECTORY")
    dist = Path(sys.argv[1]).resolve()
    wheels = sorted(dist.glob("portable_tempdirs-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one portable_tempdirs wheel in {dist}")
    root = Path(tempfile.mkdtemp(prefix="portable-tempdirs-wheel-"))
    try:
        prefix = root / "install-prefix"
        purelib = Path(sysconfig.get_path("purelib", vars={"base": str(prefix), "platbase": str(prefix)}))
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir",
             "--prefix", str(prefix), str(wheels[0])], env=environment)
        code = (
            "import importlib.metadata as metadata, sys; from pathlib import Path; "
            "import portable_tempdirs; from portable_tempdirs import temporary_directory; "
            "install=Path(sys.argv[1]).resolve(); "
            "assert Path(portable_tempdirs.__file__).resolve().is_relative_to(install); "
            "assert metadata.version('portable-tempdirs') == '0.1.0'; "
            "assert not metadata.distribution('portable-tempdirs').requires; "
            "assert set(portable_tempdirs.__all__) == {'TemporaryDirectory', 'TemporaryDirectoryError', 'temporary_directory'}; "
            "d=temporary_directory('WheelInstall'); p=Path(d.name); "
            "(p/'check.txt').write_text('wheel', encoding='utf-8'); "
            "assert (p/'check.txt').read_text(encoding='utf-8') == 'wheel'; "
            "d.cleanup(); assert not p.exists()"
        )
        subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(purelib)!r}); {code}",
                        str(purelib)], cwd=root, env=environment, check=True)
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            # Do not mask the installation/import failure or change permissions
            # to force cleanup in restricted environments.
            print(f"wheel verification workspace retained after cleanup denial: {type(exc).__name__}",
                  file=sys.stderr)
    print("isolated wheel install: OK")


if __name__ == "__main__":
    main()
