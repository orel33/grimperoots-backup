#!/usr/bin/env python3
"""Commande principale des sauvegardes et copies locales Joomla 6."""

import argparse
import datetime as dt
import ftplib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from backup_ids import IDENTIFIER, display_ids, long_id, resolve

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
BACKUP = ROOT / 'backup'
SCRIPTS = ROOT / 'scripts'


def identifier_arg(value):
    if not IDENTIFIER.fullmatch(value):
        raise argparse.ArgumentTypeError('format attendu : AAAAMMJJ ou AAAAMMJJ-HHMMSS')
    pattern = '%Y%m%d-%H%M%S' if '-' in value else '%Y%m%d'
    try:
        dt.datetime.strptime(value, pattern)
    except ValueError:
        raise argparse.ArgumentTypeError('date/heure invalide dans l’identifiant') from None
    return value


def known_runs():
    if not RUNTIME.is_dir():
        return []
    known = []
    for path in RUNTIME.iterdir():
        if not path.is_dir() or path.is_symlink() or not (path / 'run.json').is_file():
            continue
        try:
            metadata = json.loads((path / 'run.json').read_text())
            if long_id(metadata.get('archive', '')) == path.name:
                known.append(path)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(known)


def select_run(identifier):
    if not identifier or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Un ID AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list est obligatoire.')
    row = resolve(identifier, run_inventory(), 'instance locale')
    if row is None:
        raise ValueError('Utiliser un identifiant AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list.')
    path = row['path']
    if path.is_symlink() or not path.is_dir() or not (path / 'run.json').is_file():
        raise ValueError('Instance introuvable pour l’identifiant : ' + identifier)
    metadata = json.loads((path / 'run.json').read_text())
    if long_id(metadata.get('archive', '')) != path.name:
        raise ValueError('L’identifiant du dossier ne correspond pas à son archive : ' + path.name)
    if not re.fullmatch(r'grimperoots-snapshot-[a-z0-9_-]+', metadata.get('project', '')):
        raise ValueError('Métadonnées inattendues pour l’instance : ' + path.name)
    if not (path / 'compose.yaml').is_file() or not (path / '.env').is_file():
        raise ValueError('Compose ou .env absent pour l’instance : ' + path.name)
    return path, metadata


def select_backup(name):
    row = resolve(name, [row for row in local_inventory() if row['complete']], 'archive locale complète')
    if row is None:
        raise ValueError('Utiliser un identifiant AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list.')
    return row['path']


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


def remote_inventory():
    from backup_common import BackupError
    from download_archive import connect, listing, settings

    rows, errors = [], []
    directory = '.env'
    try:
        config = settings()
        directory = config['AKEEBA_REMOTE_DIR']
        ftp = connect(config)
        try:
            entries = listing(ftp, directory)
        finally:
            ftp.close()
        for name, meta in entries.items():
            if not re.fullmatch(r'.+\.(?:jpa|jps|zip)', name, re.IGNORECASE):
                continue
            stem, extension = name.rsplit('.', 1)
            prefix = 'z' if extension.lower() == 'zip' else 'j'
            pattern = re.compile(re.escape(stem) + r'\.' + prefix + r'(\d{2,})$', re.IGNORECASE)
            numbers = sorted(int(match[1]) for part in entries
                             if (match := pattern.fullmatch(part)))
            members = [part for part in entries if pattern.fullmatch(part)] + [name]
            rows.append({'archive': name, 'directory': directory,
                         'size': sum(entries[part]['size'] for part in members),
                         'parts': len(members), 'modified': meta['modified'],
                         'complete': numbers == list(range(1, len(numbers) + 1))})
    except (*ftplib.all_errors, OSError, ValueError, BackupError) as error:
        errors.append((directory, str(error)))
    return rows, errors


def local_inventory():
    rows = []
    if not BACKUP.is_dir():
        return rows
    for path in sorted(BACKUP.iterdir(), reverse=True):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            manifest_file = path / 'manifest.json'
            if manifest_file.is_file():
                manifest = json.loads(manifest_file.read_text())
                if long_id(manifest.get('archive', '')) != path.name:
                    continue
                parts = manifest['files']
                complete = bool(parts) and all((path / part['name']).is_file() and
                                               (path / part['name']).stat().st_size == part['size']
                                               for part in parts)
                rows.append({'archive': manifest['archive'], 'path': path,
                             'directory': manifest.get('remote_directory', '?'),
                             'size': sum(part['size'] for part in parts),
                             'status': 'complet' if complete else 'fichier absent ou taille incorrecte',
                             'complete': complete})
            elif (path / 'parts.json').is_file():
                parts = json.loads((path / 'parts.json').read_text())
                if not parts:
                    continue
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
                if long_id(archive) != path.name:
                    continue
                rows.append({'archive': archive, 'path': path,
                             'directory': parts[0]['remote'].rsplit('/', 1)[0],
                             'size': total, 'status': f'incomplet {received / total:.1%}',
                             'complete': False})
        except (OSError, ValueError, KeyError, ZeroDivisionError, TypeError, IndexError):
            continue
    return rows


def run_inventory():
    rows = []
    for path in known_runs():
        try:
            metadata = json.loads((path / 'run.json').read_text())
            if metadata.get('archive'):
                rows.append({'archive': metadata['archive'], 'path': path, 'metadata': metadata})
        except (OSError, ValueError, TypeError):
            continue
    return rows


def list_backups(args):
    remote, errors = ([], []) if args.local_only else remote_inventory()
    local = local_inventory()
    runs = run_inventory()
    displayed = ({} if errors else display_ids(row['archive'] for row in remote + local + runs))

    def identifier(row):
        stamp = long_id(row['archive'])
        return displayed.get(stamp, stamp or '—')

    print('FTP OVH')
    if args.local_only:
        print('  non consulté')
    elif not remote:
        print('  aucune archive disponible')
    for row in sorted(remote, key=lambda item: item['modified'], reverse=True):
        state = 'complet' if row['complete'] else 'partie manquante'
        print(f"  {identifier(row)}  {format_size(row['size'])}  "
              f"{row['parts']} partie(s)  {state}  {row['archive']}  ({row['directory']})")
    for directory, error in errors:
        print(f'  {directory} : accès impossible ({error})')

    print('Archives locales')
    if not local:
        print('  aucune')
    for row in local:
        print(f"  {identifier(row)}  {row['status']}  {format_size(row['size'])}  "
              f"{row['archive']}  ({row['path'].name})")

    print('Restaurations locales')
    if not runs:
        print('  aucune')
    for row in runs:
        metadata = row['metadata']
        print(f"  {identifier(row)}  {metadata.get('stage', '?')}  "
              f"{metadata.get('url', '')}  ({row['path'].name})")
    if errors:
        raise RuntimeError('Inventaire FTP impossible ; inventaire local affiché.')


def run_command(args):
    if args.action == 'download':
        command = [sys.executable, str(SCRIPTS / 'download_archive.py')]
        if args.identifier:
            rows, errors = remote_inventory()
            if errors:
                raise RuntimeError('Impossible de résoudre l’identifiant : inventaire FTP incomplet.')
            row = resolve(args.identifier, rows, 'archive FTP', allow_same_name=True)
            if row is None:
                raise ValueError('Utiliser un identifiant AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list.')
            if not row['complete']:
                raise ValueError('La série de cette archive FTP est incomplète.')
            command += ['--archive-name', row['archive']]
        subprocess.run(command, check=True)
    elif args.action == 'restore':
        command = [sys.executable, str(SCRIPTS / 'restore_archive.py')]
        if args.identifier:
            backup = select_backup(args.identifier)
        else:
            from restore_archive import latest_download
            backup = latest_download()
        matching = [row for row in run_inventory()
                    if Path(row['metadata'].get('archive_dir', '')).resolve() == backup.resolve()]
        if len(matching) > 1:
            raise ValueError('Plusieurs instances correspondent à cette archive ; examiner ./j6.sh list.')
        if matching:
            row = matching[0]
            if row['metadata'].get('stage') == 'complete':
                print('Archive déjà restaurée : ' + row['metadata'].get('url', str(row['path'])))
                return
            command += ['--resume', row['path'].name]
        else:
            command.append(str(backup))
        subprocess.run(command, check=True)
    elif args.action == 'instances':
        runs = run_inventory()
        if not runs:
            print('Aucune restauration locale sous runtime/.')
        displayed = display_ids(row['archive'] for row in runs)
        for row in runs:
            _, metadata = select_run(long_id(row['archive']))
            print(f"{displayed.get(long_id(row['archive']), '?')}\t"
                  f"{metadata.get('stage', '?')}\t{metadata.get('url', '')}")
    elif args.action in ('start', 'stop', 'restart'):
        run_dir, metadata = select_run(args.identifier)
        if args.action == 'restart':
            compose(run_dir, metadata, 'stop')
            compose(run_dir, metadata, 'up', '-d', '--wait')
        else:
            compose(run_dir, metadata, *{'start': ('up', '-d', '--wait'),
                                         'stop': ('stop',)}[args.action])
    elif args.action == 'status':
        run_dir, metadata = select_run(args.identifier)
        compose(run_dir, metadata, 'ps')
    elif args.action == 'verify':
        run_dir, metadata = select_run(args.identifier)
        compose(run_dir, metadata, 'up', '-d', '--wait')
        from verify_restore import verify_run
        verify_run(run_dir, int(metadata['port']))
    elif args.action == 'clean':
        if not args.identifier:
            raise ValueError('Un identifiant est obligatoire pour clean.')
        run_dir, metadata = select_run(args.identifier)
        print(f'Suppression de l’instance {run_dir} et de son volume MySQL ; archives conservées.')
        confirm('CLEAN ' + args.identifier)
        compose(run_dir, metadata, 'down', '--volumes', '--remove-orphans')
        shutil.rmtree(run_dir)
    elif args.action == 'cleanall':
        backups = local_inventory()
        runs = run_inventory()
        selected = resolve(args.identifier, backups + runs, 'backup local', allow_same_name=True)
        if selected is None:
            raise ValueError('Utiliser un identifiant AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list.')
        archive = selected['archive']
        local_paths = [row['path'] for row in backups if row['archive'] == archive]
        run_rows = [row for row in runs if row['archive'] == archive]
        if any(path.is_symlink() or path.resolve().parent != RUNTIME.resolve() for path in
               (row['path'] for row in run_rows)):
            raise ValueError('Instance hors du dossier attendu ; cleanall annulé.')
        if any(path.is_symlink() or path.resolve().parent != BACKUP.resolve() for path in local_paths):
            raise ValueError('Archive locale hors du dossier attendu ; cleanall annulé.')
        print(f'Suppression de {len(run_rows)} instance(s), de leurs volumes MySQL et de '
              f'{len(local_paths)} téléchargement(s) local(aux) pour {archive}.')
        confirm('CLEANALL ' + args.identifier)
        for row in run_rows:
            run_dir, metadata = select_run(row['path'].name)
            compose(run_dir, metadata, 'down', '--volumes', '--remove-orphans')
            shutil.rmtree(run_dir)
        for path in local_paths:
            shutil.rmtree(path)
    elif args.action == 'diskusage':
        if args.identifier:
            local = [row for row in local_inventory() if long_id(row['archive']) and
                     args.identifier in (long_id(row['archive']), long_id(row['archive'])[:8])]
            runs = [row for row in run_inventory() if long_id(row['archive']) and
                    args.identifier in (long_id(row['archive']), long_id(row['archive'])[:8])]
            resolve(args.identifier, local + runs, 'backup local', allow_same_name=True)
            paths = [row['path'] for row in local + runs]
        else:
            paths = (BACKUP, RUNTIME)
        for path in paths:
            print(f'{format_size(disk_size(path))}\t{path}')
        print('Volumes et images Docker exclus.')
    elif args.action == 'list':
        list_backups(args)


def main():
    parser = argparse.ArgumentParser(
        prog='./j6.sh',
        description='Télécharger, restaurer et gérer les sauvegardes Joomla 6.',
        epilog='''Exemples :
  ./j6.sh list
  ./j6.sh download 20261010
  ./j6.sh restore 20261010
  ./j6.sh instances
  ./j6.sh status 20261010
  ./j6.sh start 20261010
  ./j6.sh verify 20261010
  ./j6.sh cleanall 20261010''',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True, metavar='commande')
    download = sub.add_parser('download', help='télécharger la dernière archive ou celle désignée par ID')
    download.add_argument('identifier', nargs='?', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    restore = sub.add_parser('restore', help='restaurer la dernière archive locale ou celle désignée par ID')
    restore.add_argument('identifier', nargs='?', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    start = sub.add_parser('start', help='démarrer les conteneurs de ID')
    start.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    sub.add_parser('instances', help='lister les instances restaurées et leur étape')
    status = sub.add_parser('status', help='afficher l’état des conteneurs de ID')
    status.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    stop = sub.add_parser('stop', help='arrêter les conteneurs de ID')
    stop.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    restart = sub.add_parser('restart', help='redémarrer les conteneurs de ID')
    restart.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    verify = sub.add_parser('verify', help='vérifier le site restauré pour ID')
    verify.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    clean = sub.add_parser('clean', help='supprimer une instance en conservant son archive locale')
    clean.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    cleanall = sub.add_parser('cleanall', help='supprimer le backup local ID, son instance et son volume')
    cleanall.add_argument('identifier', type=identifier_arg, help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    usage = sub.add_parser('diskusage', help='afficher la taille des sauvegardes et instances')
    usage.add_argument('identifier', nargs='?', type=identifier_arg, help='limiter à ce backup local')
    inventory = sub.add_parser('list', help='inventaire FTP, archives locales et instances restaurées')
    inventory.add_argument('--local-only', action='store_true', help='inventaire local sans connexion FTP')
    args = parser.parse_args()
    try:
        run_command(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Échec : ' + str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
