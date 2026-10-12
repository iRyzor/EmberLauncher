import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from protocol_profile import load_profile
from stove_linux.http_context import NativeHTTPContext
from stove_linux.version_context import NativeVersionContext


class ProtocolProfileTests(unittest.TestCase):
    @patch(
        "protocol_profile.NativeHTTPContext.from_installation",
        return_value=NativeHTTPContext("a" * 40),
    )
    @patch(
        "protocol_profile.NativeVersionContext.from_installation",
        return_value=NativeVersionContext(b"b" * 64),
    )
    def test_profile_is_reusable_without_official_executable(self, version, http):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation = SimpleNamespace(root=root / "installation", prefix=root / "prefix")
            (installation.root / "STOVE").mkdir(parents=True)
            (installation.root / "STOVE/STOVE.exe").touch()
            installation.prefix.mkdir()
            (installation.prefix / "user.reg").touch()
            first = load_profile(installation, root / "config")
            version.side_effect = AssertionError(
                "Official executable must not be read twice"
            )
            http.side_effect = AssertionError(
                "Official executable must not be read twice"
            )
            second = load_profile(installation, root / "config")
            self.assertEqual(first[0].caller_detail, second[0].caller_detail)
            self.assertNotEqual(first[0].transaction_id, second[0].transaction_id)
            files = list((root / "config").glob("*.json"))
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertNotIn("access_token", files[0].read_text())
            self.assertEqual(http.call_count, 1)
