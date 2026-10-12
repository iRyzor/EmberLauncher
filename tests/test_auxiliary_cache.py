import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
import auxiliary_cache as auxiliary
from stove_linux.auth import AuthError


class AuxiliaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = b"validated component"
        for name, value in (
            ("EXPECTED_SIZE", len(self.data)),
            ("EXPECTED_HASH", hashlib.sha256(self.data).hexdigest()),
        ):
            patcher = patch.object(auxiliary, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def populate(self):
        source = self.root / "source"
        source.write_bytes(self.data)
        auxiliary.copy_verified(source, auxiliary.cache_path(self.root / "config"))

    def test_cache_survives_source_removal_and_installs_exact_bytes(self):
        self.populate()
        (self.root / "source").unlink()
        auxiliary.install(self.root / "build", 55, self.root / "config")
        self.assertEqual(
            (self.root / "build" / auxiliary.RELATIVE).read_bytes(), self.data
        )

    def test_missing_or_corrupt_cache_never_installs(self):
        with self.assertRaises(AuthError):
            auxiliary.install(self.root / "build", 55, self.root / "config")
        self.populate()
        auxiliary.cache_path(self.root / "config").write_bytes(b"corrupt")
        with self.assertRaises(AuthError):
            auxiliary.install(self.root / "build", 55, self.root / "config")
        self.assertFalse((self.root / "build").exists())

    def test_existing_different_component_is_preserved(self):
        self.populate()
        target = self.root / "build" / auxiliary.RELATIVE
        target.parent.mkdir(parents=True)
        target.write_bytes(b"preserve")
        with self.assertRaises(AuthError):
            auxiliary.install(self.root / "build", 55, self.root / "config")
        self.assertEqual(target.read_bytes(), b"preserve")
        self.assertEqual(list(target.parent.glob(".ember-aux-*")), [])

    def test_symlinked_cache_is_rejected(self):
        self.populate()
        source = auxiliary.cache_path(self.root / "config")
        source.unlink()
        source.symlink_to(self.root / "source")
        with self.assertRaises(AuthError):
            auxiliary.install(self.root / "build", 55, self.root / "config")

    def test_other_distribution_is_rejected(self):
        self.populate()
        with self.assertRaises(AuthError):
            auxiliary.install(self.root / "build", 56, self.root / "config")
        self.assertFalse((self.root / "build").exists())
