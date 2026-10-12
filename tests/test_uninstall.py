from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from oracle_engine import Engine
from stove_linux.auth import AuthError
from stove_linux.installation import Installation
from uninstall import uninstall


class UninstallTests(unittest.TestCase):
    def test_engine_success_returns_to_fresh_destination(self):
        import time

        def state(installation):
            return {"distribution_version": 55 if self.game.exists() else None}

        def remove(installation, confirmed_path):
            self.assertEqual(confirmed_path, str(self.game))
            self.game.rename(self.root / "trash")
            return {"uninstalled": True, "trashed": True}

        with (
            patch("oracle_engine.installed_update_state", side_effect=state),
            patch("oracle_engine.uninstall", side_effect=remove),
        ):
            engine = Engine(str(self.installation.root))
            engine.settings_path = self.root / "config/settings.json"
            engine.dispatch("uninstall", {"path": str(self.game), "confirmed": True})
            for _ in range(100):
                if not engine.job["running"]:
                    break
                time.sleep(0.005)
            snapshot = engine.snapshot()
            self.assertFalse(snapshot["job"]["running"])
            self.assertTrue(snapshot["job"]["result"]["uninstalled"])
            self.assertFalse(snapshot["game_installed"])
            self.assertTrue(snapshot["fresh_destination"])
            self.assertIsNone(snapshot["version"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / "prefix"
        (self.prefix / "drive_c").mkdir(parents=True)
        (self.prefix / "system.reg").write_text("preserve prefix")
        self.installation = Installation.discover(self.prefix / "drive_c/Smilegate")
        self.game = self.installation.game_dir
        (self.game / "bin").mkdir(parents=True)
        (self.game / "bin/ucldr_ChaosZeroNightmare_GL_loader_x64.exe").write_bytes(
            b"test"
        )
        (self.game.parent / "OtherGame").mkdir()
        (self.game.parent / "OtherGame/keep").write_text("preserve")
        patcher = patch(
            "uninstall.installed_update_state",
            return_value={"distribution_version": 55},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_selected_game_moves_to_trash_and_prefix_is_preserved(self):
        def move(arguments, **kwargs):
            self.assertEqual(arguments, ["/usr/bin/gio", "trash", "--", str(self.game)])
            self.game.rename(self.root / "trash")
            return SimpleNamespace(returncode=0)

        with (
            patch("uninstall.shutil.which", return_value="/usr/bin/gio"),
            patch("uninstall.subprocess.run", side_effect=move),
        ):
            result = uninstall(self.installation, str(self.game))
        self.assertTrue(result["trashed"])
        self.assertFalse(self.game.exists())
        self.assertEqual((self.prefix / "system.reg").read_text(), "preserve prefix")
        self.assertEqual((self.game.parent / "OtherGame/keep").read_text(), "preserve")
        self.assertTrue(
            (
                self.root / "trash/bin/ucldr_ChaosZeroNightmare_GL_loader_x64.exe"
            ).is_file()
        )

    def test_wrong_confirmation_never_invokes_trash(self):
        with patch("uninstall.subprocess.run") as run:
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game.parent))
            run.assert_not_called()
        self.assertTrue(self.game.is_dir())

    def test_symlinked_game_never_invokes_trash(self):
        outside = self.root / "outside"
        self.game.rename(outside)
        self.game.symlink_to(outside, target_is_directory=True)
        with patch("uninstall.subprocess.run") as run:
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game))
            run.assert_not_called()
        self.assertTrue(outside.is_dir())

    def test_running_game_and_pending_transaction_never_invoke_trash(self):
        with (
            patch("uninstall.require_idle", side_effect=ValueError("busy")),
            patch("uninstall.subprocess.run") as run,
        ):
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game))
            run.assert_not_called()
        journal = self.game / ".stove-update-transaction"
        journal.mkdir()
        (journal / "journal.json").write_text('{"phase":"applying"}')
        with patch("uninstall.subprocess.run") as run:
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game))
            run.assert_not_called()

    def test_missing_trash_service_and_trash_failure_preserve_game(self):
        with patch("uninstall.shutil.which", return_value=None):
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game))
        with patch(
            "uninstall.subprocess.run", return_value=SimpleNamespace(returncode=1)
        ):
            with self.assertRaises(AuthError):
                uninstall(self.installation, str(self.game))
        self.assertTrue(self.game.is_dir())

    def test_engine_requires_confirmation_logout_and_finished_jobs(self):
        with patch(
            "oracle_engine.installed_update_state", return_value={"distribution_version": 55}
        ):
            engine = Engine(str(self.installation.root))
        with patch("oracle_engine.uninstall") as remove:
            self.assertIn(
                "error", engine.dispatch("uninstall", {"path": str(self.game)})
            )
            engine.session = object()
            self.assertIn(
                "error",
                engine.dispatch(
                    "uninstall", {"path": str(self.game), "confirmed": True}
                ),
            )
            engine.session = None
            engine.job = {"kind": "prepare", "running": True}
            self.assertIn(
                "error",
                engine.dispatch(
                    "uninstall", {"path": str(self.game), "confirmed": True}
                ),
            )
            remove.assert_not_called()
