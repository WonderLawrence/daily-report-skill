"""Installation checks use isolated directories; no global skills are touched."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # Adds the workspace scripts directory to the test import path.
import sync_install


class TestInstallSync(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'workspace'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('new skill')
        (self.source / 'scripts').mkdir()
        (self.source / 'scripts/collect.py').write_text('new collector')
        self.destination = self.base / 'installed/daily-report'

    def sync(self, apply=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return sync_install.sync(self.source, self.destination, apply)

    def test_check_is_read_only_and_reports_missing_or_changed_files(self):
        self.assertEqual(self.sync(), 1)
        self.assertFalse(self.destination.parent.exists())
        self.assertEqual(self.sync(True), 0)
        before = (self.destination / 'SKILL.md').stat().st_mtime_ns
        self.assertEqual(self.sync(), 0)
        self.assertEqual((self.destination / 'SKILL.md').stat().st_mtime_ns, before)
        (self.source / 'SKILL.md').write_text('changed')
        self.assertEqual(self.sync(), 1)
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'new skill')

    def test_replacement_preserves_backup_and_excludes_caches_and_git_data(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'SKILL.md').write_text('old skill')
        (self.destination / 'obsolete.py').write_text('old module')
        (self.source / '.git').mkdir()
        (self.source / '.git/config').write_text('fixture configuration')
        (self.source / 'scripts/__pycache__').mkdir()
        (self.source / 'scripts/__pycache__/collect.pyc').write_bytes(b'cache')
        self.assertEqual(self.sync(True), 0)
        self.assertFalse((self.destination / 'obsolete.py').exists())
        self.assertFalse((self.destination / '.git').exists())
        self.assertFalse((self.destination / 'scripts/__pycache__').exists())
        backup = next((self.destination.parent / '.daily-report-backups').iterdir())
        self.assertEqual((backup / 'obsolete.py').read_text(), 'old module')
        self.assertEqual((backup / 'SKILL.md').read_text(), 'old skill')
        manifest = json.loads((self.destination / sync_install.MANIFEST).read_text())
        self.assertEqual(manifest['sha256'], sync_install.installed_hashes(self.destination))

    def test_failed_publish_restores_previous_installation(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'SKILL.md').write_text('old skill')
        rename = Path.rename

        def fail_staged_publish(path, target):
            if path.name.startswith('.daily-report-stage-'):
                raise OSError('fixture publish failure')
            return rename(path, target)

        with patch.object(Path, 'rename', fail_staged_publish), self.assertRaises(OSError):
            self.sync(True)
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'old skill')
        self.assertEqual(list(self.destination.parent.glob('.daily-report-stage-*')), [])

    def test_overlapping_or_unrelated_destinations_are_rejected(self):
        for destination in (self.source, self.source / 'installed', self.base):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                sync_install.sync(self.source, destination, True)
        unrelated = self.base / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'user.txt').write_text('keep')
        with self.assertRaises(ValueError):
            sync_install.sync(self.source, unrelated, True)
        self.assertEqual((unrelated / 'user.txt').read_text(), 'keep')
