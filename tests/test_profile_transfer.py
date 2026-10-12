import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from protocol_profile import transfer_profile, load_profile
from stove_linux.http_context import NativeHTTPContext
from stove_linux.version_context import NativeVersionContext


class ProfileTransferTests(unittest.TestCase):
    def test_new_root_reuses_validated_profile_without_native_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = SimpleNamespace(root=root / "reference")
            destination = SimpleNamespace(root=root / "new-root")
            config = root / "config"
            http = NativeHTTPContext("a" * 40)
            version = NativeVersionContext(b"b" * 64)
            with patch("protocol_profile.load_profile", return_value=(http, version)):
                transfer_profile(source, destination, config)
            recovered_http, recovered_version = load_profile(destination, config)
            self.assertEqual(recovered_http.caller_detail, http.caller_detail)
            self.assertEqual(recovered_version.suffix, version.suffix)
            path = next(config.glob("protocol-*.json"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                set(json.loads(path.read_text())),
                {"profile_hash", "caller_detail", "version_suffix"},
            )
            self.assertFalse(destination.root.exists())
