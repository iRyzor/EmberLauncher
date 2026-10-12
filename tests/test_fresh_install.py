import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from fresh_install import install_prepared, validate_destination, publish_no_replace
from resumable_updates import prepare_resumable
from stove_linux.auth import AuthError
from stove_linux.game_auth import GAME_ID
from stove_linux.manifest import repair_plan
from stove_linux.updates import installed_update_state, PublishedVersion


class FreshInstallTests(unittest.TestCase):
    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_installation_needs_no_auxiliary_cache_and_adds_no_component(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation, prepared, raw = self.fixture(root)
            result = install_prepared(installation, prepared)
            self.assertTrue(result["installed"])
            self.assertFalse(
                (installation.game_dir / "bin/custodiap/jacksparrow_x64.smt").exists()
            )
            self.assertFalse((root / "config").exists())
            self.assertTrue((prepared / "0.bin").is_file())
            self.assertEqual(
                list(installation.game_dir.parent.glob(".ember-install-*")), []
            )

    def fixture(self, root):
        prefix = root / "prefix"
        (prefix / "drive_c").mkdir(parents=True)
        (prefix / "system.reg").touch()
        installation = validate_destination(prefix / "drive_c/New/Smilegate")
        folder = root / "stage"
        folder.mkdir()
        files = [
            "BaseSDK.dll",
            "ucldr_ChaosZeroNightmare_GL_loader_x64.exe",
            "ssr-stove-shield.exe",
        ]
        md5 = hashlib.md5(b"abc").hexdigest()
        records = ["1|D|/bin"] + [
            f"{i}|F|/bin/{name}|55|{i}|gz|3|12|{md5}|{'0' * 32}|a|"
            for i, name in enumerate(files, 2)
        ]
        raw = json.dumps(
            {
                "service_code": GAME_ID,
                "version_no": 55,
                "extract_size": 9,
                "packed_size": 36,
                "files": records,
            }
        ).encode()
        (folder / "manifest.json").write_bytes(raw)
        empty = root / "empty"
        empty.mkdir()
        plan = repair_plan(empty, raw, 55)
        staged = []
        for index, name in enumerate(files):
            path = folder / f"{index}.bin"
            path.write_bytes(b"abc")
            staged.append({"path": "bin/" + name, "staged_file": path.name})
        plan.update(
            distribution_version=0,
            published_version=55,
            staged=staged,
            installation_root=str(installation.game_dir.resolve()),
        )
        (folder / "prepared.json").write_text(json.dumps(plan))
        return installation, folder, raw

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_fresh_distribution_published_with_native_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))
            self.assertFalse(installation.game_dir.exists())
            result = install_prepared(installation, prepared)
            self.assertTrue(result["installed"])
            self.assertEqual(result["verified_files"], 3)
            self.assertTrue(installation.executable.is_file())
            state = installed_update_state(installation)
            self.assertEqual(state["distribution_version"], 55)
            self.assertEqual(state["tracked_files"], 3)
            self.assertEqual(state["tracked_bytes"], 9)
            metadata = json.loads(
                next(
                    (installation.game_dir / "combinedata_manifest").glob("*.upf")
                ).read_text()
            )
            self.assertEqual(
                metadata["install_path"],
                installation.windows_path(installation.game_dir),
            )
            self.assertEqual(
                list(installation.game_dir.parent.glob(".ember-install-*")), []
            )

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_failure_before_publication_leaves_destination_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))
            with patch(
                "fresh_install.write_metadata", side_effect=OSError("disk error")
            ):
                with self.assertRaises(OSError):
                    install_prepared(installation, prepared)
            self.assertFalse(installation.game_dir.exists())
            self.assertEqual(
                list(installation.game_dir.parent.glob(".ember-install-*")), []
            )
            self.assertTrue((prepared / "0.bin").exists())

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_destination_created_during_publication_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))

            def race(source, destination):
                destination.mkdir()
                (destination / "existing").write_bytes(b"preserve")
                publish_no_replace(source, destination)

            with patch("fresh_install.publish_no_replace", side_effect=race):
                with self.assertRaises(FileExistsError):
                    install_prepared(installation, prepared)
            self.assertEqual(
                (installation.game_dir / "existing").read_bytes(), b"preserve"
            )
            self.assertFalse((installation.game_dir / "bin").exists())

    def test_new_sdk_is_rejected_before_destination_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))
            with self.assertRaises(AuthError):
                install_prepared(installation, prepared)
            self.assertFalse(installation.game_dir.exists())

    def test_existing_or_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation, prepared, raw = self.fixture(root)
            installation.game_dir.mkdir(parents=True)
            (installation.game_dir / "safe").write_bytes(b"safe")
            with self.assertRaises(AuthError):
                validate_destination(installation.root)
            self.assertEqual((installation.game_dir / "safe").read_bytes(), b"safe")
            other = installation.prefix / "drive_c/Other/Smilegate"
            other.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (other / "Games").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(AuthError):
                validate_destination(other)

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_tampered_file_never_becomes_an_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))
            (prepared / "1.bin").write_bytes(b"xyz")
            with self.assertRaises(ValueError):
                install_prepared(installation, prepared)
            self.assertFalse(installation.game_dir.exists())

    def test_download_preparation_leaves_fresh_destination_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation, prepared, raw = self.fixture(root)

            def stage(item, folder, *args):
                folder.mkdir()
                target = folder / "verified.bin"
                target.write_bytes(b"abc")
                return target

            with (
                patch("resumable_updates.fetch_manifest", return_value=raw),
                patch("resumable_updates.stage_resumable", side_effect=stage),
            ):
                result = prepare_resumable(
                    installation,
                    PublishedVersion(0, 55, "private-query"),
                    root / "downloads",
                )
            self.assertFalse(installation.game_dir.exists())
            self.assertEqual(result["staged_packages"], 3)
            value = json.loads(
                (Path(result["prepared_directory"]) / "prepared.json").read_text()
            )
            self.assertEqual(value["installation_root"], str(installation.game_dir))
            self.assertNotIn("private-query", json.dumps(value))

    @patch("fresh_install.SDK_HASH", hashlib.sha256(b"abc").hexdigest())
    def test_preparation_for_another_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            installation, prepared, raw = self.fixture(Path(folder))
            value = json.loads((prepared / "prepared.json").read_text())
            value["installation_root"] = "/another/game"
            (prepared / "prepared.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                install_prepared(installation, prepared)
            self.assertFalse(installation.game_dir.exists())
