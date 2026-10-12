import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles/vendor"))
from stove_linux.platform_fs import os as fs_os, publish_no_replace, path_without_links
from stove_linux.pipe_io import PipeReader, write_windows
from stove_linux.installation_lock import acquire
from protocol_profile import bootstrap_profile
import test_fresh_install
from fresh_install import install_prepared
import hashlib


class PortableRuntimeTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'POSIX macOS system aliases')
    def test_macos_system_alias_normalizes_but_custom_link_stays_rejected(self):
        import stat
        from types import SimpleNamespace
        def inspect(path):
            name = str(path)
            if name in ('/var', '/private/var/custom'):
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            return SimpleNamespace(st_mode=stat.S_IFDIR)
        with patch('stove_linux.platform_fs.sys.platform', 'darwin'), \
             patch('stove_linux.platform_fs.native_os.lstat', side_effect=inspect), \
             patch('stove_linux.platform_fs.native_os.path.realpath', return_value='/private/var'):
            self.assertEqual(path_without_links('/var/folders/test/new'), Path('/private/var/folders/test/new'))
            with self.assertRaises(ValueError):
                path_without_links('/var/custom/new')
        with patch('stove_linux.platform_fs.sys.platform', 'darwin'), \
             patch('stove_linux.platform_fs.native_os.lstat', side_effect=inspect), \
             patch('stove_linux.platform_fs.native_os.path.realpath', return_value='/unexpected'):
            with self.assertRaises(ValueError):
                path_without_links('/var/folders/test/new')

    def test_link_in_ancestor_is_rejected_for_missing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'real').mkdir()
            try:
                (root / 'alias').symlink_to(root / 'real', target_is_directory=True)
            except OSError as error:
                self.skipTest(f'Symlink creation unavailable: {error.errno}')
            with self.assertRaises(ValueError):
                path_without_links(root / 'alias/not-created/Smilegate')
            self.assertFalse((root / 'real/not-created').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 8.3 path aliases')
    def test_short_path_alias_supports_directory_lock_and_fresh_destination(self):
        import ctypes
        from ctypes import wintypes
        from fresh_install import validate_destination
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetShortPathNameW.restype = wintypes.DWORD
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve() / 'Ember validation long directory'
            root.mkdir()
            buffer = ctypes.create_unicode_buffer(32768)
            count = kernel.GetShortPathNameW(str(root), buffer, len(buffer))
            if not 0 < count < len(buffer):
                self.skipTest('Short names unavailable on this volume')
            alias = Path(buffer.value)
            if alias == root:
                self.skipTest('8.3 name generation disabled on this volume')
            self.assertEqual(path_without_links(alias), root)
            first = acquire(alias)
            try:
                with self.assertRaises(OSError):
                    acquire(root)
            finally:
                fs_os.close(first)
            installation = validate_destination(alias / 'new/Smilegate')
            self.assertEqual(installation.root, root / 'new/Smilegate')
            self.assertFalse(installation.root.exists())
            from bootstrap import create_prefix
            created = create_prefix(alias / "native-library", "native")
            self.assertEqual(created.root, root / "native-library/Smilegate")
            self.assertTrue((root / "native-library/.ember-prefix.json").is_file())

    def test_no_replace_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            (root / "target").mkdir()
            (root / "target/keep").write_text("preserve")
            with self.assertRaises(OSError):
                publish_no_replace(root / "source", root / "target")
            self.assertEqual((root / "target/keep").read_text(), "preserve")
            self.assertTrue((root / "source").is_dir())

    def test_lock_prevents_second_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            first = acquire(Path(folder))
            try:
                with self.assertRaises(OSError):
                    acquire(Path(folder))
            finally:
                os.close(first)

    def test_native_directory_descriptor_walk_and_file_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = fs_os.open(
                folder, fs_os.O_RDONLY | fs_os.O_DIRECTORY | fs_os.O_NOFOLLOW
            )
            try:
                fs_os.mkdir("bin", dir_fd=root)
                child = fs_os.open(
                    "bin",
                    fs_os.O_RDONLY | fs_os.O_DIRECTORY | fs_os.O_NOFOLLOW,
                    dir_fd=root,
                )
                try:
                    fd = fs_os.open(
                        "new",
                        fs_os.O_CREAT
                        | fs_os.O_EXCL
                        | fs_os.O_WRONLY
                        | fs_os.O_NOFOLLOW,
                        0o600,
                        dir_fd=child,
                    )
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(b"abc")
                        stream.flush()
                        fs_os.fsync(stream.fileno())
                    fs_os.replace("new", "final", src_dir_fd=child, dst_dir_fd=child)
                    self.assertEqual(
                        fs_os.stat(
                            "final", dir_fd=child, follow_symlinks=False
                        ).st_size,
                        3,
                    )
                finally:
                    fs_os.close(child)
            finally:
                fs_os.close(root)
            self.assertEqual((Path(folder) / "bin/final").read_bytes(), b"abc")

    def test_threaded_pipe_io_handles_eof_and_large_frame(self):
        read, write = os.pipe()
        with (
            os.fdopen(read, "rb", buffering=0) as input_stream,
            os.fdopen(write, "wb", buffering=0) as output_stream,
        ):
            reader = PipeReader(input_stream, 1024)
            value = b"public synthetic frame" * 1000
            write_windows(output_stream, value, threading.Event())
            result = b""
            while len(result) < len(value):
                chunk = reader.read(1)
                self.assertIsNotNone(chunk)
                result += chunk
            self.assertEqual(result, value)
            output_stream.close()
            self.assertEqual(reader.read(1), b"")
            reader.close()

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_fresh_install_native_publish_and_profile_without_stove(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation, prepared, raw = test_fresh_install.FreshInstallTests().fixture(root)
            bootstrap_profile(installation, root / "config")
            result = install_prepared(installation, prepared)
            self.assertTrue(result["installed"])
            self.assertTrue(installation.executable.is_file())
            self.assertFalse((installation.root / "STOVE").exists())
            self.assertFalse(
                (installation.game_dir / "bin/custodiap/jacksparrow_x64.smt").exists()
            )


if __name__ == "__main__":
    unittest.main()
