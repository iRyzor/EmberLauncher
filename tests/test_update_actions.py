import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
import json
import tempfile
import unittest
from unittest.mock import patch
from update_actions import (
    apply_update,
    recover_update,
    recovery_needed,
    prepared_summary,
)
from fixtures import commit_fixture
from stove_linux.transaction import FileTransaction


class UpdateActionTests(unittest.TestCase):
    def fixture(self, root):
        game, prepared, installation = commit_fixture(root)
        receipt = json.loads((prepared / "prepared.json").read_text())
        from stove_linux.manifest import repair_plan

        plan = repair_plan(game, (prepared / "manifest.json").read_bytes(), 55)
        receipt.update(entries=plan["entries"], download_bytes=plan["download_bytes"])
        (prepared / "prepared.json").write_text(json.dumps(receipt))
        return game, prepared, installation

    @patch("update_actions.require_idle")
    @patch("stove_linux.update_guard.require_idle")
    def test_review_apply_and_archive_real_files(self, guard, idle):
        with tempfile.TemporaryDirectory() as directory:
            game, prepared, installation = self.fixture(Path(directory))
            summary = prepared_summary(prepared)
            self.assertEqual(summary["change_count"], 3)
            result = apply_update(installation, prepared)
            self.assertTrue(result["applied"])
            self.assertEqual(result["version"], 55)
            self.assertEqual((game / "bin/test.dll").read_bytes(), b"abc")
            self.assertFalse(recovery_needed(installation))
            self.assertTrue(Path(result["backup_directory"]).is_dir())

    @patch("update_actions.require_idle")
    @patch("stove_linux.update_guard.require_idle")
    def test_recover_interruption_through_application_facade(self, guard, idle):
        with tempfile.TemporaryDirectory() as directory:
            game, prepared, installation = self.fixture(Path(directory))
            transaction = FileTransaction(game, prepared)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    transaction.apply(
                        55,
                        lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
                        installation,
                    )
            finally:
                transaction.close()
            self.assertTrue(recovery_needed(installation))
            result = recover_update(installation)
            self.assertTrue(result["recovered"])
            self.assertEqual(result["version"], 54)
            self.assertEqual((game / "bin/test.dll").read_bytes(), b"old")
            self.assertFalse(recovery_needed(installation))

    @patch("update_actions.require_idle", side_effect=ValueError("running"))
    def test_active_game_blocks_apply_without_writes(self, idle):
        with tempfile.TemporaryDirectory() as directory:
            game, prepared, installation = self.fixture(Path(directory))
            with self.assertRaises(ValueError):
                apply_update(installation, prepared)
            self.assertEqual((game / "bin/test.dll").read_bytes(), b"old")

    def test_manifest_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            game, prepared, installation = self.fixture(Path(directory))
            (prepared / "manifest.json").write_text("{}")
            with self.assertRaises(ValueError):
                prepared_summary(prepared)

    @patch("update_actions.require_idle")
    @patch("stove_linux.update_guard.require_idle")
    def test_corrupt_backup_blocks_recovery_before_restoring_files(self, guard, idle):
        with tempfile.TemporaryDirectory() as directory:
            game, prepared, installation = self.fixture(Path(directory))
            transaction = FileTransaction(game, prepared)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    transaction.apply(
                        55,
                        lambda item: (
                            (_ for _ in ()).throw(KeyboardInterrupt())
                            if item["path"] == "bin/test.dll"
                            else None
                        ),
                        installation,
                    )
            finally:
                transaction.close()
            (game / ".stove-update-transaction/0").write_bytes(b"corrupt backup")
            with self.assertRaises(ValueError):
                recover_update(installation)
            self.assertEqual((game / "bin/test.dll").read_bytes(), b"abc")
            self.assertTrue(recovery_needed(installation))
