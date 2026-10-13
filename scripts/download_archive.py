#!/usr/bin/env python3
"""Télécharge par FTP la dernière archive Akeeba J6 terminée et la vérifie."""

import argparse
import datetime as dt
import ftplib
import json
import os
from pathlib import Path
import re
import sys

from backup_common import BackupError, download, save_json, sha256
from backup_ids import long_id

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / '.env'


def settings():
    if not ENV_FILE.is_file():
        raise BackupError('.env absent : copier .env.example vers .env et compléter les accès FTP.')
    values = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        values[key] = value if key == 'AKEEBA_REMOTE_DIR' else os.environ.get(key, value)
    missing = [key for key in ('FTP_HOST', 'FTP_USER', 'FTP_PASSWORD', 'AKEEBA_REMOTE_DIR')
               if not values.get(key)]
    if missing:
        raise BackupError('Paramètre(s) FTP manquant(s) dans .env : ' + ', '.join(missing))
    directory = values['AKEEBA_REMOTE_DIR'].strip()
    if not directory.startswith('/') or '..' in Path(directory).parts or any(c in directory for c in '\r\n\x00'):
        raise BackupError('Le dossier Akeeba doit être un chemin FTP absolu sans segments « .. ».')
    values['FTP_PORT'] = int(values.get('FTP_PORT', '21'))
    values['FTP_TIMEOUT'] = int(values.get('TIMEOUT', '120'))
    values['FTP_PLAIN'] = values.get('FTP_PLAIN', 'true').lower() in ('1', 'true', 'yes')
    values['AKEEBA_REMOTE_DIR'] = directory.rstrip('/')
    return values


def connect(cfg):
    cls = ftplib.FTP if cfg['FTP_PLAIN'] else ftplib.FTP_TLS
    ftp = cls(timeout=cfg['FTP_TIMEOUT'])
    ftp.connect(cfg['FTP_HOST'], cfg['FTP_PORT'])
    ftp.login(cfg['FTP_USER'], cfg['FTP_PASSWORD'])
    if not cfg['FTP_PLAIN']:
        ftp.prot_p()
    ftp.set_pasv(True)
    ftp.voidcmd('TYPE I')
    return ftp


def listing(ftp, directory):
    entries = {}
    for path in ftp.nlst(directory):
        name = path.rsplit('/', 1)[-1]
        if not name or name in ('.', '..'):
            continue
        remote = directory + '/' + name
        try:
            size = ftp.size(remote)
            modified = ftp.sendcmd('MDTM ' + remote).split()[-1]
        except ftplib.all_errors:
            continue
        if size is not None:
            entries[name] = {'size': int(size), 'modified': modified}
    return entries


def latest_archive(entries):
    candidates = []
    for name, meta in entries.items():
        if re.fullmatch(r'.+\.(?:jpa|jps|zip)', name, re.IGNORECASE):
            try:
                when = dt.datetime.strptime(meta['modified'], '%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            candidates.append((when, name))
    if not candidates:
        raise BackupError('Aucune archive .jpa, .jps ou .zip terminée trouvée dans AKEEBA_REMOTE_DIR.')
    return max(candidates)


def archive_signature(ftp, directory, archive):
    stem, extension = archive.rsplit('.', 1)
    part_ext = 'z' if extension.lower() == 'zip' else 'j'
    pattern = re.compile(re.escape(stem) + r'\.' + part_ext + r'\d{2,}', re.IGNORECASE)
    entries = listing(ftp, directory)
    names = [name for name in entries if name == archive or pattern.fullmatch(name)]
    if archive not in names:
        raise BackupError('L’archive la plus récente a disparu pendant le contrôle.')
    return {name: entries[name] for name in sorted(names)}


def matching_downloads(root, parts, remote_dir):
    """Find complete and interrupted transfers of this exact remote archive."""
    expected = [(part['name'], part['size'], part['remote']) for part in parts]
    complete, partial = [], []
    if not root.is_dir():
        return complete, partial
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        parts_file = directory / 'parts.json'
        if not parts_file.is_file():
            continue
        try:
            saved = json.loads(parts_file.read_text())
            if [(p['name'], p['size'], p['remote']) for p in saved] != expected:
                continue
            manifest_file = directory / 'manifest.json'
            if manifest_file.is_file():
                manifest = json.loads(manifest_file.read_text())
                if manifest.get('remote_directory') != remote_dir:
                    continue
                files = manifest.get('files', [])
                if ([(p['name'], p['size']) for p in files] == [(p['name'], p['size']) for p in parts] and
                        all((directory / p['name']).is_file() and
                            (directory / p['name']).stat().st_size == p['size'] and
                            sha256(directory / p['name']) == p['sha256'] for p in files)):
                    complete.append(directory)
            else:
                partial.append(directory)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return complete, partial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-name', help='nom exact d’une archive .jpa, .jps ou .zip sur le FTP')
    parser.add_argument('--output-root', type=Path, default=ROOT / 'backup')
    parser.add_argument('--resume', type=Path, help='reprendre un transfert interrompu dans un dossier daté existant')
    args = parser.parse_args()
    if args.archive_name and (Path(args.archive_name).name != args.archive_name or
                              not re.fullmatch(r'.+\.(?:jpa|jps|zip)', args.archive_name, re.IGNORECASE)):
        raise BackupError('--archive-name doit être un nom de fichier Akeeba, sans chemin.')
    cfg = settings()
    os.umask(0o077)
    ftp = None
    try:
        ftp = connect(cfg)
        first = listing(ftp, cfg['AKEEBA_REMOTE_DIR'])
        archive = args.archive_name or latest_archive(first)[1]
        if archive not in first:
            raise BackupError('Archive absente du dossier FTP : ' + archive)
        identifier = long_id(archive)
        if not identifier:
            raise BackupError('Impossible de déduire un identifiant AAAAMMJJ-HHMMSS du nom de l’archive.')
        before = archive_signature(ftp, cfg['AKEEBA_REMOTE_DIR'], archive)
        parts = []
        for name in sorted(before):
            remote = cfg['AKEEBA_REMOTE_DIR'] + '/' + name
            if before[name]['size'] <= 0:
                raise BackupError('Taille distante invalide : ' + name)
            parts.append({'name': name, 'size': before[name]['size'], 'remote': remote})
        expected = [p['name'] for p in parts]
        stem, extension = archive.rsplit('.', 1)
        part_char = 'z' if extension.lower() == 'zip' else 'j'
        numbers = sorted(int(match.group(1)) for name in expected
                         if (match := re.fullmatch(re.escape(stem) + r'\.' + part_char + r'(\d{2,})', name, re.IGNORECASE)))
        if numbers and numbers != list(range(1, len(numbers) + 1)):
            raise BackupError('La série multipartite est incomplète ; aucune partie n’a été téléchargée.')
        if len(expected) != len(numbers) + 1:
            raise BackupError('Le dossier contient des éléments Akeeba inattendus pour cette archive.')

        output_root = args.output_root.resolve()
        complete, partial = matching_downloads(output_root, parts, cfg['AKEEBA_REMOTE_DIR'])
        matching = [path for path in complete if path.name == identifier]
        if matching:
            print('Archive déjà téléchargée : ' + str(matching[0]) + ' ; aucun nouveau transfert.')
            return
        output = output_root / identifier
        if args.resume:
            resume = args.resume.resolve()
            if resume != output.resolve():
                raise BackupError('--resume doit désigner backup/ID pour cette archive.')
            if not output.is_dir() or (output / 'manifest.json').exists() or not (output / 'parts.json').is_file():
                raise BackupError('--resume attend un dossier existant avec parts.json et sans manifest final.')
            old_parts = json.loads((output / 'parts.json').read_text())
            comparable = lambda rows: [(item['name'], int(item['size']), item['remote']) for item in rows]
            if comparable(old_parts) != comparable(parts):
                raise BackupError('Le transfert interrompu correspond à une autre archive, un autre dossier FTP ou ses tailles ont changé.')
        elif partial:
            output = next((path for path in partial if path.name == identifier), None)
            if output is None:
                raise BackupError('Un téléchargement partiel existe, mais pas sous son identifiant attendu : ' + identifier)
            print('Reprise automatique du transfert : ' + str(output), flush=True)
        else:
            output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if output.exists():
                raise BackupError('Le dossier backup/' + identifier + ' existe sans correspondre à cette archive.')
            output.mkdir(parents=True, exist_ok=False, mode=0o700)
            save_json(output / 'parts.json', parts)
        completed = download(ftp, parts, output)
        after = archive_signature(ftp, cfg['AKEEBA_REMOTE_DIR'], archive)
        if before != after:
            raise BackupError('L’archive ou une de ses parties a changé pendant le téléchargement ; les fichiers partiels sont conservés, relancer après stabilisation.')
        manifest = {
            'source': 'ftp-ovh-j6', 'remote_directory': cfg['AKEEBA_REMOTE_DIR'],
            'archive': archive, 'files': completed,
            'downloaded_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'total_bytes': sum(item['size'] for item in completed),
        }
        save_json(output / 'manifest.json', manifest)
        (output / 'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['name']}\n" for item in completed))
        (output / 'SHA256SUMS').chmod(0o600)
        print(f"Archive J6 rapatriée et vérifiée : {output} ({manifest['total_bytes'] / 1e9:.2f} Go).", flush=True)
        identifier = long_id(archive)
        if identifier:
            print('Utiliser ensuite ./j6.sh restore ' + identifier + '. Le backup distant est conservé.')
        else:
            print('Consulter ./j6.sh list pour la restauration. Le backup distant est conservé.')
    finally:
        if ftp is not None:
            ftp.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterruption ; relancer avec --resume sur le même dossier pour reprendre.', file=sys.stderr)
        raise SystemExit(130)
    except (BackupError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        print('Échec : ' + message, file=sys.stderr)
        raise SystemExit(1)
    except ftplib.all_errors:
        print('Échec de la connexion FTP ou du transfert ; fichiers partiels conservés.', file=sys.stderr)
        raise SystemExit(1)
