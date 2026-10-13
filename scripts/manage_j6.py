#!/usr/bin/env python3
"""Commande principale des sauvegardes et copies locales Joomla 6."""

import argparse
import ftplib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'j6/runs'
BACKUP = ROOT / 'j6/backup/akeeba'
SCRIPTS = ROOT / 'scripts'
PRODUCTION_REMOTE_DIR = '/backup/j6-latest'
REFERENCE_REMOTE_DIR = '/backup/j6-20261004'


def known_runs():
    if not RUNS.is_dir():
        return []
    return sorted(path for path in RUNS.iterdir()
                  if path.is_dir() and not path.is_symlink() and (path / 'run.json').is_file())


def select_run(name):
    if name is None:
        runs = known_runs()
        if len(runs) != 1:
            raise ValueError('Préciser le nom du run (./j6.sh status affiche les runs disponibles).')
        path = runs[0]
    else:
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('Le run doit être un nom sous j6/runs/, sans chemin.')
        path = RUNS / name
        if path.is_symlink() or not path.is_dir() or not (path / 'run.json').is_file():
            raise ValueError('Run introuvable : ' + name)
    metadata = json.loads((path / 'run.json').read_text())
    if metadata.get('project') != 'grimperoots-snapshot-' + path.name.lower().replace('_', '-'):
        raise ValueError('Métadonnées de run inattendues : ' + path.name)
    if not (path / 'compose.yaml').is_file() or not (path / '.env').is_file():
        raise ValueError('Compose ou .env absent pour le run : ' + path.name)
    return path, metadata


def select_backup(name):
    path = Path(name)
    if path.is_dir() and (path / 'manifest.json').is_file():
        return path.resolve()
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('Utiliser un identifiant local affiché par ./j6.sh list.')
    path = BACKUP / name
    if (path / 'manifest.json').is_file():
        return path
    matches = []
    if BACKUP.is_dir():
        for directory in BACKUP.iterdir():
            manifest_path = directory / 'manifest.json'
            if not directory.is_dir() or not manifest_path.is_file():
                continue
            try:
                if json.loads(manifest_path.read_text()).get('archive') == name:
                    matches.append(directory)
            except (OSError, ValueError):
                continue
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError('Plusieurs téléchargements portent ce nom ; utiliser leur identifiant local affiché par list.')
    raise ValueError('Archive locale introuvable : ' + name)


def compose(run_dir, metadata, *args):
    command = ['sg', 'docker', '-c', shlex.join([
        'docker', 'compose', '--env-file', str(run_dir / '.env'),
        '-p', metadata['project'], '-f', str(run_dir / 'compose.yaml'), *args])]
    subprocess.run(command, check=True)


def confirm(expected):
    if not sys.stdin.isatty():
        raise ValueError('Confirmation refusée : un terminal interactif est requis.')
    print(f'Pour confirmer la suppression locale, saisir exactement : {expected}')
    if input('> ') != expected:
        raise ValueError('Annulé.')


def disk_size(path):
    if not path.exists():
        return 0
    return sum(file.stat().st_size for file in path.rglob('*') if file.is_file() and not file.is_symlink())


def format_size(size):
    for unit in ('o', 'Kio', 'Mio', 'Gio', 'Tio'):
        if size < 1024 or unit == 'Tio':
            return f'{size:.1f} {unit}'
        size /= 1024


def list_backups(args):
    if not args.local_only:
        from download_archive import connect, listing, settings
        from backup_common import BackupError
        directories = [args.remote_dir] if args.remote_dir else [PRODUCTION_REMOTE_DIR, REFERENCE_REMOTE_DIR]
        remote_count = len(directories)
        print('FTP OVH')
        errors = []
        for directory in directories:
            try:
                config = settings(directory)
                ftp = connect(config)
                try:
                    entries = listing(ftp, config['AKEEBA_REMOTE_DIR'])
                finally:
                    ftp.close()
                print('  ' + config['AKEEBA_REMOTE_DIR'])
                archives = [(name, meta) for name, meta in entries.items()
                            if re.fullmatch(r'.+\.(?:jpa|jps|zip)', name, re.IGNORECASE)]
                if not archives:
                    print('    aucune archive finale')
                for name, meta in sorted(archives, key=lambda item: item[1]['modified'], reverse=True):
                    stem, extension = name.rsplit('.', 1)
                    prefix = 'z' if extension.lower() == 'zip' else 'j'
                    pattern = re.compile(re.escape(stem) + r'\.' + prefix + r'(\d{2,})$', re.IGNORECASE)
                    numbered = sorted(int(match[1]) for part in entries
                                      if (match := pattern.fullmatch(part)))
                    complete = numbered == list(range(1, len(numbered) + 1))
                    members = [part for part in entries if pattern.fullmatch(part)] + [name]
                    size = sum(entries[part]['size'] for part in members)
                    label = 'série complète' if complete else 'partie manquante'
                    remote_id = 'ftp:' + config['AKEEBA_REMOTE_DIR'] + '/' + name
                    print(f"    {remote_id}  {format_size(size)}  {len(members)} partie(s)  "
                          f"{meta['modified']} UTC  {label}")
            except (*ftplib.all_errors, OSError, ValueError, BackupError) as error:
                print(f'  {directory} : accès impossible ({error})')
                errors.append(str(error))
    else:
        errors = []
        remote_count = 0

    print('Archives locales')
    if not BACKUP.is_dir():
        print('  aucune')
    else:
        directories = sorted((path for path in BACKUP.iterdir() if path.is_dir()), reverse=True)
        if not directories:
            print('  aucune')
        for path in directories:
            try:
                manifest_file = path / 'manifest.json'
                if manifest_file.is_file():
                    manifest = json.loads(manifest_file.read_text())
                    parts = manifest['files']
                    complete = all((path / part['name']).is_file() and
                                   (path / part['name']).stat().st_size == part['size'] for part in parts)
                    status = 'complet (tailles vérifiées)' if complete else 'fichier absent ou taille incorrecte'
                    print(f"  {path.name}  {manifest['archive']}  {format_size(sum(p['size'] for p in parts))}  "
                          f"{status}  {manifest.get('remote_directory', '?')}")
                elif (path / 'parts.json').is_file():
                    parts = json.loads((path / 'parts.json').read_text())
                    total = sum(part['size'] for part in parts)
                    received = 0
                    for part in parts:
                        file = path / part['name']
                        partial = path / (part['name'] + '.partial')
                        if file.is_file():
                            received += min(file.stat().st_size, part['size'])
                        elif partial.is_file():
                            received += min(partial.stat().st_size, part['size'])
                    archive = next((part['name'] for part in parts
                                    if re.search(r'\.(?:jpa|jps|zip)$', part['name'], re.IGNORECASE)), parts[0]['name'])
                    print(f"  {path.name}  {archive}  incomplet "
                          f"{received / total:.1%}  {parts[-1]['remote'].rsplit('/', 1)[0]}")
            except (OSError, ValueError, KeyError, ZeroDivisionError, TypeError) as error:
                print(f'  {path.name}  métadonnées illisibles : {error}')

    print('Restaurations locales')
    runs = known_runs()
    if not runs:
        print('  aucune')
    for path in runs:
        try:
            metadata = json.loads((path / 'run.json').read_text())
            print(f"  {path.name}  {metadata.get('stage', '?')}  {metadata.get('url', '')}")
        except (OSError, ValueError) as error:
            print(f'  {path.name}  métadonnées illisibles : {error}')
    if remote_count and len(errors) == remote_count:
        raise RuntimeError('Inventaire FTP impossible ; inventaire local affiché.')


def run_command(args):
    if args.action == 'download':
        command = [sys.executable, str(SCRIPTS / 'download_archive.py')]
        if args.archive and args.archive.startswith('ftp:'):
            if args.reference or args.remote_dir:
                raise ValueError('Un identifiant ftp: contient déjà son dossier ; retirer --reference/--remote-dir.')
            remote = args.archive[4:]
            directory, separator, archive = remote.rpartition('/')
            if not separator or not directory.startswith('/') or not archive:
                raise ValueError('Identifiant FTP invalide : ' + args.archive)
            command += ['--remote-dir', directory, '--archive-name', archive]
        elif args.archive:
            command += ['--archive-name', args.archive]
        if args.reference and not (args.archive and args.archive.startswith('ftp:')):
            command += ['--remote-dir', REFERENCE_REMOTE_DIR]
        elif args.remote_dir:
            command += ['--remote-dir', args.remote_dir]
        elif not (args.archive and args.archive.startswith('ftp:')):
            command += ['--remote-dir', PRODUCTION_REMOTE_DIR]
        if args.resume:
            command += ['--resume', str(args.resume)]
        subprocess.run(command, check=True)
    elif args.action == 'restore':
        command = [sys.executable, str(SCRIPTS / 'restore_archive.py')]
        if args.resume_run:
            run_dir, _ = select_run(args.resume_run)
            command += ['--resume-run', str(run_dir)]
        elif args.backup or args.archive:
            command.append(str(select_backup(args.backup or str(args.archive))))
        else:
            command.append('--latest')
        if args.port is not None:
            command += ['--port', str(args.port)]
        subprocess.run(command, check=True)
    elif args.action == 'status' and args.run is None:
        runs = known_runs()
        if not runs:
            print('Aucune restauration locale sous j6/runs/.')
        for path in runs:
            _, metadata = select_run(path.name)
            print(f"{path.name}\t{metadata.get('stage', '?')}\t{metadata.get('url', '')}")
            compose(path, metadata, 'ps')
    elif args.action in ('start', 'stop', 'restart', 'status'):
        run_dir, metadata = select_run(args.run)
        if args.action == 'restart':
            compose(run_dir, metadata, 'stop')
            compose(run_dir, metadata, 'up', '-d', '--wait')
        else:
            compose(run_dir, metadata, *{'start': ('up', '-d', '--wait'),
                                         'stop': ('stop',), 'status': ('ps',)}[args.action])
    elif args.action == 'verify':
        run_dir, metadata = select_run(args.run)
        compose(run_dir, metadata, 'up', '-d', '--wait')
        from verify_restore import verify_run
        verify_run(run_dir, int(metadata['port']))
    elif args.action == 'clean':
        run_dir, metadata = select_run(args.run)
        print(f'Suppression de {run_dir} et de son volume MySQL ; archives conservées.')
        confirm('CLEAN ' + run_dir.name)
        compose(run_dir, metadata, 'down', '--volumes', '--remove-orphans')
        shutil.rmtree(run_dir)
    elif args.action == 'cleanall':
        if RUNS.is_dir():
            known = set(known_runs())
            unknown = [path.name for path in RUNS.iterdir() if path not in known]
            if unknown:
                raise ValueError('Dossiers de runs non reconnus ; cleanall annulé : ' + ', '.join(unknown))
        runs = [select_run(path.name) for path in known_runs()]
        print(f'Suppression de {len(runs)} run(s), de leurs volumes MySQL et de {BACKUP}.')
        confirm('CLEANALL j6')
        for run_dir, metadata in runs:
            compose(run_dir, metadata, 'down', '--volumes', '--remove-orphans')
            shutil.rmtree(run_dir)
        if BACKUP.exists():
            shutil.rmtree(BACKUP)
    elif args.action == 'diskusage':
        for path in (BACKUP, RUNS):
            print(f'{format_size(disk_size(path))}\t{path}')
        print('Volumes et images Docker exclus.')
    elif args.action == 'list':
        list_backups(args)


def main():
    parser = argparse.ArgumentParser(prog='./j6.sh', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    download = sub.add_parser('download', help='télécharger la dernière archive Akeeba J6')
    download.add_argument('archive', nargs='?', help='identifiant ftp: affiché par list ou nom exact d’archive')
    source = download.add_mutually_exclusive_group()
    source.add_argument('--latest', action='store_true', help='alias du dossier de production par défaut')
    source.add_argument('--reference', action='store_true', help='archive de référence J6 du 4 octobre 2026')
    source.add_argument('--remote-dir', help='autre dossier FTP absolu')
    download.add_argument('--resume', type=Path, help='dossier de transfert incomplet ; reprise automatique sinon')
    restore = sub.add_parser('restore', help='restaurer la dernière archive locale dans un nouveau run')
    restore.add_argument('backup', nargs='?', help='identifiant local ou nom d’archive affiché par list')
    selection = restore.add_mutually_exclusive_group()
    selection.add_argument('--archive', type=Path, help='dossier de téléchargement à restaurer')
    selection.add_argument('--resume-run', help='nom du run à reprendre')
    restore.add_argument('--port', type=int, help='port HTTP local (attribué automatiquement sinon)')
    for action in ('start', 'status', 'stop', 'restart', 'clean', 'verify'):
        item = sub.add_parser(action)
        item.add_argument('run', nargs='?', help='nom du run sous j6/runs/')
    sub.add_parser('cleanall', help='supprimer tous les runs, volumes et archives locales')
    sub.add_parser('diskusage', help='afficher la taille des archives et runs')
    inventory = sub.add_parser('list', help='inventaire FTP, archives locales et restaurations')
    inventory.add_argument('--local-only', action='store_true', help='inventaire local sans connexion FTP')
    inventory.add_argument('--remote-dir', help='limiter l’inventaire FTP à ce dossier')
    args = parser.parse_args()
    if args.action == 'download' and args.archive and args.latest:
        parser.error('choisir un nom d’archive ou --latest')
    if args.action == 'restore' and args.backup and (args.archive or args.resume_run):
        parser.error('choisir un backup, --archive ou --resume-run')
    if args.action == 'restore' and args.resume_run and args.port is not None:
        parser.error('--port ne se combine pas avec --resume-run')
    try:
        run_command(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Échec : ' + str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
