import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from bootstrap import create_prefix
from protocol_profile import bootstrap_profile, load_profile
from stove_linux.auth import AuthError


class BootstrapTests(unittest.TestCase):
    def test_profile_is_independent_stable_and_contains_no_account_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation = SimpleNamespace(root=root / "absent-installation")
            with patch(
                "protocol_profile.NativeHTTPContext.from_installation",
                side_effect=AssertionError("No official binary"),
            ):
                first = bootstrap_profile(installation, root / "config")
                second = load_profile(installation, root / "config")
                third = bootstrap_profile(installation, root / "config")
            self.assertEqual(first[0].caller_detail, second[0].caller_detail)
            self.assertEqual(first[0].caller_detail, third[0].caller_detail)
            self.assertEqual(len(first[0].caller_detail), 40)
            self.assertEqual(len(first[1].suffix), 64)
            path = next((root / "config").glob("protocol-*"))
            self.assertEqual(
                set(json.loads(path.read_text())),
                {"caller_detail", "profile_hash", "version_suffix"},
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(installation.root.exists())

    def test_existing_foreign_directory_is_never_modified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "keep").write_text("original")
            with (
                patch("bootstrap.shutil.which", return_value="/usr/bin/wine"),
                patch("bootstrap.subprocess.run") as run,
            ):
                with self.assertRaises(AuthError):
                    create_prefix(root, "wine")
                run.assert_not_called()
            self.assertEqual((root / "keep").read_text(), "original")
            self.assertFalse((root / ".ember-prefix.json").exists())

    def test_new_prefix_uses_its_own_environment_and_can_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "prefix"

            def boot(args, **kwargs):
                self.assertEqual(args, ["/usr/bin/wine", "wineboot.exe", "--init"])
                self.assertEqual(kwargs["env"]["WINEPREFIX"], str(root))
                self.assertEqual(kwargs["env"]["WINEARCH"], "win64")
                (root / "drive_c/windows").mkdir(parents=True, exist_ok=True)
                (root / "system.reg").touch()
                return SimpleNamespace(returncode=0)

            with (
                patch("bootstrap.shutil.which", return_value="/usr/bin/wine"),
                patch("bootstrap.subprocess.run", side_effect=boot),
            ):
                first = create_prefix(root, "wine")
                second = create_prefix(root, "wine")
            self.assertEqual(first.root, root / "drive_c/Smilegate")
            self.assertEqual(first.root, second.root)
            self.assertFalse(first.game_dir.exists())

    def test_symlink_and_missing_wine_are_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(AuthError):
                create_prefix(root / "link/prefix", "wine")
            with patch("bootstrap.shutil.which", return_value=None):
                with self.assertRaises(AuthError):
                    create_prefix(root / "missing", "wine")
            self.assertFalse((root / "missing").exists())

    def test_failed_boot_keeps_only_owned_prefix_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / "prefix"
            with (
                patch("bootstrap.shutil.which", return_value="/usr/bin/wine"),
                patch(
                    "bootstrap.subprocess.run",
                    return_value=SimpleNamespace(returncode=1),
                ),
            ):
                with self.assertRaises(AuthError):
                    create_prefix(prefix, "wine")
            self.assertEqual(
                json.loads((prefix / ".ember-prefix.json").read_text())["owner"],
                "ember-launcher",
            )
