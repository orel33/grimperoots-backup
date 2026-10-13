"""Checks for archive identity, selection, and safe restoration recovery."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import download_archive
import manage_j6
import restore_archive


class Workflows(unittest.TestCase):
    def test_complete_transfer_is_not_reused_if_corrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / 'one'
            directory.mkdir()
            parts = [{'name': 'site.jpa', 'size': 3, 'remote': '/backup/j6-latest/site.jpa'}]
            (directory / 'parts.json').write_text(json.dumps(parts))
            (directory / 'site.jpa').write_bytes(b'bad')
            manifest = {'remote_directory': '/backup/j6-latest',
                        'files': [{'name': 'site.jpa', 'size': 3,
                                   'sha256': hashlib.sha256(b'good').hexdigest()}]}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            complete, partial = download_archive.matching_downloads(root, parts, '/backup/j6-latest')
            self.assertEqual((complete, partial), ([], []))
            (directory / 'site.jpa').write_bytes(b'yes')
            manifest['files'][0]['sha256'] = hashlib.sha256(b'yes').hexdigest()
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            complete, partial = download_archive.matching_downloads(root, parts, '/backup/j6-latest')
            self.assertEqual((complete, partial), ([directory], []))

    def test_archive_identity_includes_every_part(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {'archive': 'site.jpa', 'files': [
                {'name': 'site.j01', 'sha256': hashlib.sha256(b'first').hexdigest()},
                {'name': 'site.jpa', 'sha256': hashlib.sha256(b'last').hexdigest()}]}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            first = restore_archive.archive_sha256(root)
            manifest['files'][0]['sha256'] = hashlib.sha256(b'changed').hexdigest()
            (root / 'manifest.json').write_text(json.dumps(manifest))
            self.assertNotEqual(first, restore_archive.archive_sha256(root))

    def test_local_backup_identifier_selects_correct_archive(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(manage_j6, 'BACKUP', Path(temp)):
            for identifier in ('one', 'two'):
                path = Path(temp) / identifier
                path.mkdir()
                (path / 'manifest.json').write_text(json.dumps({'archive': identifier + '.jpa'}))
            self.assertEqual(manage_j6.select_backup('two'), Path(temp) / 'two')
            self.assertEqual(manage_j6.select_backup('one.jpa'), Path(temp) / 'one')

    def test_ftp_identifier_routes_to_its_directory(self):
        args = type('Args', (), {'action': 'download', 'archive': 'ftp:/backup/j6-latest/site.jpa',
                                  'reference': False, 'remote_dir': None, 'resume': None})()
        with mock.patch.object(manage_j6.subprocess, 'run') as run:
            manage_j6.run_command(args)
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ['--remote-dir', '/backup/j6-latest', '--archive-name', 'site.jpa'])

    def test_download_defaults_to_production_directory(self):
        args = type('Args', (), {'action': 'download', 'archive': None,
                                  'reference': False, 'remote_dir': None, 'resume': None})()
        with mock.patch.object(manage_j6.subprocess, 'run') as run:
            manage_j6.run_command(args)
        self.assertEqual(run.call_args.args[0][-2:], ['--remote-dir', '/backup/j6-latest'])

    def test_verification_failure_can_resume_without_reimport(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            metadata = {'port': 8081, 'stage': 'files_extracted', 'url': 'http://localhost:8081/j6/'}
            with mock.patch.object(restore_archive, 'restore_database_and_finish') as database, \
                 mock.patch.object(restore_archive, 'verify_run', side_effect=[RuntimeError('HTTP failed'), None]) as verify, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'HTTP failed'):
                    restore_archive.finish_run(run_dir, metadata)
                self.assertEqual(json.loads((run_dir / 'run.json').read_text())['stage'], 'verifying')
                restore_archive.finish_run(run_dir, metadata)
            database.assert_called_once()
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(json.loads((run_dir / 'run.json').read_text())['stage'], 'complete')

    def test_generated_compose_uses_relocated_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / 'run'
            run_dir.mkdir()
            _, runtime = restore_archive.compose_file(run_dir, Path(temp) / 'archive', 18081)
            compose = (run_dir / 'compose.yaml').read_text()
            self.assertIn(str(restore_archive.ROOT / 'j6/docker/apache.conf'), compose)
            self.assertIn(str(restore_archive.ROOT / 'j6/docker/proxy.conf'), compose)
            self.assertIn(str(restore_archive.KICKSTART), compose)
            self.assertEqual(runtime, run_dir / 'runtime')

    def test_cleanall_refuses_unknown_run_directory(self):
        args = type('Args', (), {'action': 'cleanall'})()
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(manage_j6, 'RUNS', Path(temp) / 'runs'), \
             mock.patch.object(manage_j6, 'BACKUP', Path(temp) / 'backup'):
            (manage_j6.RUNS / 'unknown').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'non reconnus'):
                manage_j6.run_command(args)

    def test_list_prints_actionable_remote_identifier(self):
        class FTP:
            def close(self):
                pass

        args = type('Args', (), {'local_only': False, 'remote_dir': '/backup/j6-latest'})()
        entries = {'site.jpa': {'size': 12, 'modified': '20261013120000'}}
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(manage_j6, 'BACKUP', Path(temp) / 'backup'), \
             mock.patch.object(manage_j6, 'RUNS', Path(temp) / 'runs'), \
             mock.patch.object(download_archive, 'settings', return_value={'AKEEBA_REMOTE_DIR': '/backup/j6-latest'}), \
             mock.patch.object(download_archive, 'connect', return_value=FTP()), \
             mock.patch.object(download_archive, 'listing', return_value=entries), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            manage_j6.list_backups(args)
        self.assertIn('ftp:/backup/j6-latest/site.jpa', output.getvalue())


if __name__ == '__main__':
    unittest.main()
