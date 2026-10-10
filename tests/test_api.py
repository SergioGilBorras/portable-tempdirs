from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from portable_tempdirs import TemporaryDirectoryError, temporary_directory


class PortableTempdirsTests(unittest.TestCase):
    def test_create_io_nested_cleanup(self):
        managed = temporary_directory("PortableTests")
        root = Path(managed.name)
        (root / "nested" / "deeper").mkdir(parents=True)
        (root / "nested" / "deeper" / "sample.txt").write_text("value", encoding="utf-8")
        self.assertEqual((root / "nested" / "deeper" / "sample.txt").read_text(encoding="utf-8"), "value")
        managed.cleanup()
        self.assertFalse(root.exists())

    def test_cleanup_handles_supplementary_unicode_names(self):
        managed = temporary_directory("UnicodeChildren")
        root = Path(managed.name)
        nested = root / "nested-\U0001f680"
        nested.mkdir()
        (nested / "file-\U0001f680.txt").write_text("unicode", encoding="utf-8")
        (root / "other-\U0001f680.txt").write_text("safe", encoding="utf-8")
        managed.cleanup()
        self.assertFalse(root.exists())

    def test_default_layout_and_utc_random_name(self):
        managed = temporary_directory("PortableTests")
        path = Path(managed.name)
        try:
            self.assertEqual(path.parent.name, "PortableTests")
            self.assertRegex(path.name, r"^\d{8}T\d{12}Z-[0-9a-f]{24}$")
            if os.name == "nt":
                self.assertEqual(managed.privacy_assessment["status"], "NO_VERIFICABLE")
            else:
                self.assertEqual(managed.privacy_assessment["status"], "MODE_0700")
        finally:
            managed.cleanup()

    def test_explicit_parent_is_used_directly_and_can_transfer_after_rename(self):
        parent_owner = temporary_directory("PortableParents")
        parent = Path(parent_owner.name)
        managed = temporary_directory("Consumer", parent=parent)
        staging = Path(managed.name)
        destination = parent / "workspace"
        try:
            self.assertEqual(staging.parent, parent)
            os.replace(staging, destination)
            managed.mark_moved_to(destination)
            self.assertTrue(destination.is_dir())
        finally:
            # The parent manager owns the renamed synthetic child as well.
            parent_owner.cleanup()

    def test_move_transfer_reopens_verified_parent_after_failed_cleanup(self):
        from portable_tempdirs import posix_backend, windows_backend

        parent_owner = temporary_directory("MoveRetryParent")
        parent = Path(parent_owner.name)
        managed = temporary_directory("MoveRetry", parent=parent)
        source = Path(managed.name)
        destination = parent / "moved-workspace"
        backend_module = windows_backend if os.name == "nt" else posix_backend
        patch_target = "_set_delete" if os.name == "nt" else "rmtree"
        patch_owner = backend_module if os.name == "nt" else backend_module.shutil
        try:
            with patch.object(patch_owner, patch_target, side_effect=PermissionError("synthetic denial")):
                with self.assertRaises(TemporaryDirectoryError):
                    managed.cleanup()
            os.replace(source, destination)
            managed.mark_moved_to(destination)
            self.assertTrue(destination.is_dir())
        finally:
            parent_owner.cleanup()

    def test_namespace_rejects_traversal_and_reserved_names(self):
        for invalid in ("..", "../escape", "a/b", "CON", "LPT1", "has.dot"):
            with self.subTest(namespace=invalid), self.assertRaises(ValueError):
                temporary_directory(invalid)

    def test_invalid_creation_options_fail_before_allocation(self):
        for attempts in (0, -1, True, 1.5):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                temporary_directory("ValidNamespace", collision_attempts=attempts)
        with self.assertRaises(ValueError):
            temporary_directory("ValidNamespace", parent="relative")

    def test_error_diagnostics_do_not_expose_parent_path(self):
        parent_owner = temporary_directory("DiagnosticParent")
        parent = Path(parent_owner.name)
        managed = temporary_directory("DiagnosticChild", parent=parent)
        root = Path(managed.name)
        try:
            root.rename(root.with_name(root.name + "-parked"))
            with self.assertRaises(TemporaryDirectoryError) as caught:
                managed.cleanup()
            self.assertNotIn(str(parent), repr(caught.exception.diagnostic))
        finally:
            parent_owner.cleanup()

    def test_copy_of_package_imports_without_product_modules(self):
        source = Path(__file__).resolve().parents[1] / "src" / "portable_tempdirs"
        sandbox = temporary_directory("IsolatedImport")
        root = Path(sandbox.name)
        target = root / "portable_tempdirs"
        try:
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from portable_tempdirs import temporary_directory; "
                "d=temporary_directory('CopiedPackage'); d.cleanup(); "
                "assert 'pytest' not in sys.modules; "
                "print('ISOLATED_IMPORT_OK')"
            )
            result = subprocess.run([os.sys.executable, "-I", "-c", code, str(root)],
                                    cwd=str(root), capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "ISOLATED_IMPORT_OK")
        finally:
            sandbox.cleanup()

    def test_unique_parallel_allocations(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            items = list(pool.map(lambda _: temporary_directory("ParallelTests"), range(16)))
        try:
            self.assertEqual(len({item.name for item in items}), len(items))
        finally:
            for item in items:
                item.cleanup()

    def test_collision_is_not_reused_or_deleted(self):
        parent_owner = temporary_directory("CollisionParent")
        parent = Path(parent_owner.name)
        names = iter(("20261009T120000000000Z-" + "a" * 24,
                      "20261009T120000000000Z-" + "b" * 24))
        collision = parent / next(names)
        collision.mkdir()
        marker = collision / "keep.txt"
        marker.write_text("foreign", encoding="utf-8")
        with patch("portable_tempdirs.core._leaf_name", side_effect=lambda: next(names)):
            managed = temporary_directory("Collision", parent=parent, collision_attempts=2)
        try:
            self.assertNotEqual(Path(managed.name), collision)
            self.assertEqual(marker.read_text(encoding="utf-8"), "foreign")
        finally:
            managed.cleanup()
            parent_owner.cleanup()

    def test_replaced_entry_is_rejected_and_foreign_tree_survives(self):
        parent_owner = temporary_directory("RaceParent")
        parent = Path(parent_owner.name)
        managed = temporary_directory("Race", parent=parent)
        owned = Path(managed.name)
        parked = owned.with_name(owned.name + "-parked")
        replacement_marker = owned / "foreign.txt"
        owned.rename(parked)
        owned.mkdir()
        replacement_marker.write_text("preserve", encoding="utf-8")
        try:
            with self.assertRaises(TemporaryDirectoryError) as caught:
                managed.cleanup()
            self.assertEqual(caught.exception.diagnostic["cleanup_status"], "PENDING")
            self.assertEqual(replacement_marker.read_text(encoding="utf-8"), "preserve")
        finally:
            parent_owner.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows handle-relative child open")
    def test_child_replacement_after_enumeration_is_rejected_by_file_id(self):
        from portable_tempdirs import windows_backend
        parent_owner = temporary_directory("ChildRaceParent")
        parent = Path(parent_owner.name)
        managed = temporary_directory("ChildRace", parent=parent)
        root = Path(managed.name)
        child = root / "owned.txt"
        parked = root / "owned-original.txt"
        marker = root / "owned.txt"
        child.write_text("original", encoding="utf-8")
        real_enumerate = windows_backend._enumerate
        swapped = False

        def swap_after_listing(handle, kernel):
            nonlocal swapped
            entries = real_enumerate(handle, kernel)
            if not swapped:
                swapped = True
                try:
                    child.rename(parked)
                except PermissionError:
                    raise TemporaryDirectoryError("concurrent replacement blocked by directory handle",
                                                  code="RACE_BLOCKED") from None
                marker.write_text("replacement", encoding="utf-8")
            return entries

        try:
            with patch.object(windows_backend, "_enumerate", side_effect=swap_after_listing):
                with self.assertRaises(TemporaryDirectoryError) as caught:
                    managed.cleanup()
            self.assertEqual(caught.exception.code, "RACE_BLOCKED")
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")
            self.assertFalse(parked.exists())
            managed.cleanup()
        finally:
            parent_owner.cleanup()

    def test_permission_denied_cleanup_is_reported_and_retry_is_safe(self):
        from portable_tempdirs import posix_backend, windows_backend
        managed = temporary_directory("DeniedCleanup")
        root = Path(managed.name)
        (root / "nested").mkdir()
        (root / "nested" / "data.txt").write_text("synthetic", encoding="utf-8")
        try:
            backend_module = windows_backend if os.name == "nt" else posix_backend
            target = "_set_delete" if os.name == "nt" else "rmtree"
            with patch.object(backend_module if os.name == "nt" else backend_module.shutil,
                             target, side_effect=PermissionError("synthetic denial")):
                with self.assertRaises(TemporaryDirectoryError) as caught:
                    managed.cleanup()
            self.assertEqual(caught.exception.diagnostic["cleanup_status"], "PENDING")
            self.assertTrue((root / "nested" / "data.txt").is_file())
            managed.cleanup()
            self.assertFalse(root.exists())
        finally:
            if root.exists():
                managed.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_is_rejected_without_touching_target(self):
        parent_owner = temporary_directory("JunctionParent")
        outside_owner = temporary_directory("JunctionOutside")
        parent, outside = Path(parent_owner.name), Path(outside_owner.name)
        managed = temporary_directory("Junction", parent=parent)
        root = Path(managed.name)
        junction = root / "external"
        marker = outside / "preserve.txt"
        marker.write_text("keep", encoding="utf-8")
        command = shutil.which("powershell.exe") or shutil.which("powershell")
        if not command:
            self.skipTest("PowerShell unavailable for junction fixture")
        env = os.environ.copy()
        env["PORTABLE_LINK"] = str(junction)
        env["PORTABLE_TARGET"] = str(outside)
        result = subprocess.run([command, "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:PORTABLE_LINK -Target $env:PORTABLE_TARGET -ErrorAction Stop | Out-Null"],
            capture_output=True, timeout=10, check=False, env=env)
        if result.returncode:
            self.skipTest("host did not permit creating a synthetic junction")
        try:
            with self.assertRaises(TemporaryDirectoryError):
                managed.cleanup()
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        finally:
            if junction.exists():
                os.rmdir(junction)
            if root.exists():
                managed.cleanup()
            outside_owner.cleanup()
            parent_owner.cleanup()

    def test_real_symlink_never_traverses_external_target(self):
        owner = temporary_directory("SymlinkOwner")
        outside_owner = temporary_directory("SymlinkTarget")
        root, outside = Path(owner.name), Path(outside_owner.name)
        marker = outside / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        link = root / "external-link"
        try:
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                owner.cleanup()
                outside_owner.cleanup()
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows denied symlink creation with ERROR_PRIVILEGE_NOT_HELD")
                raise
            if os.name == "nt":
                with self.assertRaises(TemporaryDirectoryError):
                    owner.cleanup()
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
                link.unlink()
                owner.cleanup()
            else:
                owner.cleanup()
                self.assertFalse(root.exists())
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
        finally:
            if root.exists():
                if link.is_symlink():
                    link.unlink()
                owner.cleanup()
            outside_owner.cleanup()

    def test_cleanup_error_is_structured_and_primary_exception_survives(self):
        managed = temporary_directory("CleanupError")
        root = Path(managed.name)
        try:
            with patch.object(managed._backend, "cleanup", side_effect=TemporaryDirectoryError(
                    "synthetic cleanup failure", code="DENIED",
                    diagnostic={"phase": "CLEANUP", "category": "DENIED", "cleanup_status": "PENDING",
                                "directory_id": root.name})):
                with self.assertRaisesRegex(RuntimeError, "primary") as raised:
                    with managed:
                        raise RuntimeError("primary")
            self.assertTrue(any("cleanup also failed" in note for note in raised.exception.__notes__))
            self.assertTrue(root.exists())
        finally:
            if root.exists():
                managed.cleanup()

    def test_permission_failure_during_self_check_cleans_without_fallback(self):
        from portable_tempdirs import posix_backend, windows_backend
        backend_type = windows_backend.WindowsDirectory if os.name == "nt" else posix_backend.PosixDirectory
        namespace = Path(__import__("tempfile").gettempdir()) / "SelfCheckFailure"
        before = set(namespace.iterdir()) if namespace.exists() else set()
        with patch.object(backend_type, "verify_io", side_effect=PermissionError("synthetic denial")):
            with self.assertRaises(TemporaryDirectoryError) as caught:
                temporary_directory("SelfCheckFailure")
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
        self.assertEqual(set(namespace.iterdir()), before)

    @unittest.skipUnless(os.name != "nt", "POSIX mode bits")
    def test_posix_mode_is_private(self):
        managed = temporary_directory("PrivateMode")
        try:
            self.assertEqual(stat.S_IMODE(Path(managed.name).stat().st_mode), 0o700)
        finally:
            managed.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows process handle accounting")
    def test_windows_handles_return_to_baseline(self):
        import ctypes
        from portable_tempdirs import windows_backend

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [ctypes.wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.wintypes.DWORD)]
        kernel.GetProcessHandleCount.restype = ctypes.wintypes.BOOL
        process = kernel.GetCurrentProcess()
        before, after = ctypes.wintypes.DWORD(), ctypes.wintypes.DWORD()
        self.assertTrue(kernel.GetProcessHandleCount(process, ctypes.byref(before)))
        for _ in range(3):
            managed = temporary_directory("HandleAccounting")
            managed.cleanup()
        with patch.object(windows_backend, "_attributes", side_effect=OSError("synthetic attribute failure")):
            with self.assertRaises(TemporaryDirectoryError):
                temporary_directory("HandleAccountingFailure")
        self.assertTrue(kernel.GetProcessHandleCount(process, ctypes.byref(after)))
        self.assertLessEqual(after.value, before.value + 1)


if __name__ == "__main__":
    unittest.main()
