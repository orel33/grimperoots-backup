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
import backup_ids
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
            for identifier, stamp in (('one', '20261010-112009'), ('two', '20261010-175542')):
                path = Path(temp) / identifier
                path.mkdir()
                (path / 'archive.jpa').write_bytes(b'a')
                (path / 'manifest.json').write_text(json.dumps({
                    'archive': 'grimperoots-j6-' + stamp + '.jpa',
                    'files': [{'name': 'archive.jpa', 'size': 1}]}))
            self.assertEqual(manage_j6.select_backup('20261010-175542'), Path(temp) / 'two')
            with self.assertRaisesRegex(ValueError, 'ambigu'):
                manage_j6.select_backup('20261010')

    def test_timestamp_identifier_routes_to_its_ftp_directory(self):
        args = type('Args', (), {'action': 'download', 'identifier': '20261004'})()
        rows = [{'archive': 'grimperoots-j6-20261004-215319.jpa',
                 'directory': '/backup/j6-20261004', 'complete': True}]
        with mock.patch.object(manage_j6, 'remote_inventory', return_value=(rows, [])), \
             mock.patch.object(manage_j6.subprocess, 'run') as run:
            manage_j6.run_command(args)
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ['--remote-dir', '/backup/j6-20261004',
                                       '--archive-name', 'grimperoots-j6-20261004-215319.jpa'])

    def test_ambiguous_short_ftp_identifier_does_not_download(self):
        args = type('Args', (), {'action': 'download', 'identifier': '20261010'})()
        rows = [{'archive': 'grimperoots-j6-20261010-112009.jpa',
                 'directory': '/backup/j6-latest', 'complete': True},
                {'archive': 'grimperoots-j6-20261010-175542.jpa',
                 'directory': '/backup/j6-latest', 'complete': True}]
        with mock.patch.object(manage_j6, 'remote_inventory', return_value=(rows, [])), \
             mock.patch.object(manage_j6.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'ambigu'):
                manage_j6.run_command(args)
            run.assert_not_called()

    def test_download_defaults_to_production_directory(self):
        args = type('Args', (), {'action': 'download', 'identifier': None})()
        with mock.patch.object(manage_j6.subprocess, 'run') as run:
            manage_j6.run_command(args)
        self.assertEqual(run.call_args.args[0][-2:], ['--remote-dir', '/backup/j6-latest'])

    def test_verification_failure_can_resume_without_reimport(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            metadata = {'port': 8081, 'stage': 'files_extracted', 'url': 'http://localhost:8081/j6/',
                        'archive': 'grimperoots-j6-20261010-112009.jpa'}
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
        args = type('Args', (), {'local_only': False})()
        rows = [{'archive': 'grimperoots-j6-20261010-112009.jpa', 'directory': '/backup/j6-latest',
                 'size': 12, 'parts': 1, 'modified': '20261010120000', 'complete': True}]
        with mock.patch.object(manage_j6, 'remote_inventory', return_value=(rows, [])), \
             mock.patch.object(manage_j6, 'local_inventory', return_value=[]), \
             mock.patch.object(manage_j6, 'run_inventory', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            manage_j6.list_backups(args)
        self.assertIn('20261010  production', output.getvalue())

    def test_list_uses_full_id_if_ftp_inventory_is_incomplete(self):
        args = type('Args', (), {'local_only': False})()
        rows = [{'archive': 'grimperoots-j6-20261010-112009.jpa', 'directory': '/backup/j6-latest',
                 'size': 12, 'parts': 1, 'modified': '20261010120000', 'complete': True}]
        with mock.patch.object(manage_j6, 'remote_inventory', return_value=(rows, [
                ('/backup/j6-20261004', 'indisponible')])), \
             mock.patch.object(manage_j6, 'local_inventory', return_value=[]), \
             mock.patch.object(manage_j6, 'run_inventory', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            manage_j6.list_backups(args)
        self.assertIn('20261010-112009  production', output.getvalue())

    def test_short_id_is_only_shown_when_day_is_unique(self):
        names = ['grimperoots-j6-20261010-112009.jpa']
        self.assertEqual(backup_ids.display_ids(names)['20261010-112009'], '20261010')
        names.append('grimperoots-j6-20261010-175542.jpa')
        self.assertEqual(backup_ids.display_ids(names)['20261010-112009'], '20261010-112009')
        with self.assertRaisesRegex(ValueError, 'ambigu'):
            backup_ids.resolve('20261010', [{'archive': name} for name in names], 'archive')

    def test_run_commands_resolve_same_backup_identifier(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(manage_j6, 'RUNS', Path(temp)):
            path = Path(temp) / 'grimperoots-j6-20261010-112009-abcd'
            path.mkdir()
            (path / 'compose.yaml').write_text('services: {}\n')
            (path / '.env').write_text('DB_PASSWORD=x\n')
            metadata = {'archive': 'grimperoots-j6-20261010-112009.jpa',
                        'project': 'grimperoots-snapshot-' + path.name}
            (path / 'run.json').write_text(json.dumps(metadata))
            self.assertEqual(manage_j6.select_run('20261010')[0], path)
            self.assertEqual(manage_j6.select_run('20261010-112009')[0], path)

    def test_restore_identifier_resumes_its_incomplete_run(self):
        args = type('Args', (), {'action': 'restore', 'identifier': '20261010'})()
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / 'backup'
            backup.mkdir()
            run_dir = Path(temp) / 'run'
            run_dir.mkdir()
            row = {'archive': 'grimperoots-j6-20261010-112009.jpa', 'path': run_dir,
                   'metadata': {'archive_dir': str(backup), 'stage': 'verifying'}}
            with mock.patch.object(manage_j6, 'select_backup', return_value=backup), \
                 mock.patch.object(manage_j6, 'run_inventory', return_value=[row]), \
                 mock.patch.object(manage_j6.subprocess, 'run') as run:
                manage_j6.run_command(args)
            self.assertEqual(run.call_args.args[0][-2:], ['--resume-run', str(run_dir)])

    def test_restore_identifier_reuses_complete_run(self):
        args = type('Args', (), {'action': 'restore', 'identifier': '20261010'})()
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / 'backup'
            backup.mkdir()
            row = {'archive': 'grimperoots-j6-20261010-112009.jpa', 'path': Path(temp) / 'run',
                   'metadata': {'archive_dir': str(backup), 'stage': 'complete',
                                'url': 'http://localhost:18081/j6/'}}
            with mock.patch.object(manage_j6, 'select_backup', return_value=backup), \
                 mock.patch.object(manage_j6, 'run_inventory', return_value=[row]), \
                 mock.patch.object(manage_j6.subprocess, 'run') as run, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                manage_j6.run_command(args)
            run.assert_not_called()
            self.assertIn('http://localhost:18081/j6/', output.getvalue())


if __name__ == '__main__':
    unittest.main()
