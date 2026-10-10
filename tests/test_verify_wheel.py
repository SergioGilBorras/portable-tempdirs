from __future__ import annotations

import subprocess
import tomllib
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from portable_tempdirs import temporary_directory


class VerifyWheelTests(unittest.TestCase):
    def test_rejects_wheel_without_temporary_directory_detach(self) -> None:
        workspace_owner = temporary_directory("StaleWheelTest")
        try:
            project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
            version = project["project"]["version"]
            wheel = Path(workspace_owner.name) / f"portable_tempdirs-{version}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    f"portable_tempdirs-{version}.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: portable-tempdirs\nVersion: {version}\n",
                )
                archive.writestr("portable_tempdirs/__init__.py", "")
                archive.writestr("portable_tempdirs/py.typed", "")
                archive.writestr(
                    "portable_tempdirs/core.py",
                    "class TemporaryDirectory:\n    def cleanup(self):\n        pass\n",
                )

            verifier = Path(__file__).with_name("verify_wheel.py")
            result = subprocess.run(
                [sys.executable, str(verifier), str(Path(workspace_owner.name))],
                cwd=workspace_owner.name,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TemporaryDirectory.detach() is missing", result.stderr)
        finally:
            workspace_owner.cleanup()


if __name__ == "__main__":
    unittest.main()
