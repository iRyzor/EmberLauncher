"""Run platform-appropriate checks with explicit import paths."""
from pathlib import Path
import sys
import unittest
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "tests/oracles"), str(root / "tests/oracles/vendor"), str(root / "tests")]
loader = unittest.TestLoader()
if sys.platform == "linux":
    suite = loader.discover(str(root / "tests"))
else:
    suite = loader.loadTestsFromNames(["test_portable_runtime", "test_native_protocol", "test_native_crypto", "test_native_auth", "test_update_actions", "test_session_access", "test_portable_processes", "test_native_process", "test_native_sdk", "test_rust_engine", "test_engine", "test_dependencies"])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
