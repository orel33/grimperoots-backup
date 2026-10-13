#!/usr/bin/env python3
"""Restaure une archive Akeeba J6 dans un nouveau Compose local isolé."""

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request

from backup_common import Page
from backup_ids import long_id, resolve
from verify_restore import verify_run

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
KICKSTART = ROOT / 'vendor/akeeba-kickstart/9.1.0'


def run_docker(command, cwd=ROOT, **kwargs):
    check = kwargs.pop('check', True)
    return subprocess.run(['sg', 'docker', '-c', command], cwd=cwd, check=check, **kwargs)


def quote(value):
    return json.dumps(str(value))


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def compose_file(instance_dir, archive_dir, port, project=None):
    runtime = instance_dir / 'www'
    project = project or ('grimperoots-snapshot-' + instance_dir.name.lower().replace('_', '-'))
    compose = f'''name: {project}
services:
  web:
    image: grimperoots-backup-php83
    volumes:
      - {quote(str(ROOT / 'j6/docker/apache.conf') + ':/etc/apache2/sites-available/000-default.conf:ro')}
      - {quote(str(runtime) + ':/var/www/html')}
      - {quote(str(KICKSTART) + ':/opt/restore:ro')}
      - {quote(str(archive_dir) + ':/opt/deployment:ro')}
    depends_on:
      db:
        condition: service_healthy
    networks: [isolated]
    security_opt: [no-new-privileges:true]
    cap_drop: [ALL]
    restart: "no"
  proxy:
    image: nginx:1.28-alpine
    ports: ["127.0.0.1:{port}:8080"]
    volumes:
      - {quote(str(ROOT / 'j6/docker/proxy.conf') + ':/etc/nginx/conf.d/default.conf:ro')}
    depends_on: [web]
    networks: [isolated, access]
    restart: "no"
  db:
    image: mysql:8.0
    environment:
      MYSQL_DATABASE: grimperoots_snapshot
      MYSQL_USER: grimperoots_snapshot
      MYSQL_PASSWORD: ${{DB_PASSWORD:?Préparer le test}}
      MYSQL_ROOT_PASSWORD: ${{DB_ROOT_PASSWORD:?Préparer le test}}
    command: [--character-set-server=utf8mb4, --collation-server=utf8mb4_unicode_ci, --max-allowed-packet=128M]
    volumes: [database:/var/lib/mysql]
    healthcheck:
      test: ["CMD-SHELL", "MYSQL_PWD=$$MYSQL_PASSWORD mysql -h 127.0.0.1 -u $$MYSQL_USER -D $$MYSQL_DATABASE -e 'SELECT 1' >/dev/null 2>&1"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 30s
    networks: [isolated]
    restart: "no"
networks:
  isolated:
    internal: true
  access:
volumes:
  database:
'''
    (instance_dir / 'compose.yaml').write_text(compose)
    (instance_dir / 'compose.yaml').chmod(0o600)
    env_file = instance_dir / '.env'
    if not env_file.exists():
        env_text = f'DB_PASSWORD={secrets.token_urlsafe(36)}\nDB_ROOT_PASSWORD={secrets.token_urlsafe(36)}\n'
        env_file.write_text(env_text)
        env_file.chmod(0o600)
    wrapper = f'''#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")"
compose=(docker compose --env-file .env -p {shlex.quote(project)} -f compose.yaml)
case "${{1:-}}" in
  start) set -- up -d --wait ;;
  status) set -- ps ;;
  stop) set -- stop ;;
  restart) "${{compose[@]}}" stop; set -- up -d --wait ;;
  *) echo "Usage: ${{0##*/}} start|status|stop|restart" >&2; exit 2 ;;
esac
exec "${{compose[@]}}" "$@"
'''
    (instance_dir / 'joomla.sh').write_text(wrapper)
    (instance_dir / 'joomla.sh').chmod(0o700)
    return project, runtime


def docker_compose(run_dir, project, action, **kwargs):
    command = ('docker compose --env-file ' + shlex.quote(str(run_dir / '.env')) +
               ' -p ' + shlex.quote(project) + ' -f ' + shlex.quote(str(run_dir / 'compose.yaml')) +
               ' ' + action)
    return run_docker(command, **kwargs)


def write_run(run_dir, metadata):
    path = run_dir / 'run.json'
    path.write_text(json.dumps(metadata, indent=2) + '\n')
    path.chmod(0o600)


def finish_run(run_dir, metadata):
    port = int(metadata['port'])
    try:
        if metadata.get('stage') != 'verifying':
            metadata['stage'] = 'restoring_database'
            write_run(run_dir, metadata)
            restore_database_and_finish(run_dir, port)
            metadata['stage'] = 'verifying'
            write_run(run_dir, metadata)
        verify_run(run_dir, port)
    except BaseException:
        (run_dir / 'restore-traceback.txt').write_text(traceback.format_exc())
        (run_dir / 'restore-traceback.txt').chmod(0o600)
        raise
    metadata['stage'] = 'complete'
    write_run(run_dir, metadata)
    print(f"Restauration terminée : {metadata['url']}", flush=True)
    print(f"Administration : {metadata['url']}administrator/", flush=True)
    identifier = long_id(metadata['archive'])
    if identifier:
        print(f"Gestion de cette instance : ./j6.sh start {identifier}, status {identifier}, "
              f"stop {identifier} ou restart {identifier}", flush=True)
    print('Courriels désactivés et noindex activé ; l’archive et les autres instances sont conservées.')


def verify_archive(archive_dir):
    manifest_path = archive_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise RuntimeError('manifest.json absent : utiliser une archive rapatriée par ./j6.sh download.')
    manifest = json.loads(manifest_path.read_text())
    archive = manifest.get('archive', '')
    if not re.fullmatch(r'.+\.(?:jpa|jps|zip)', archive, re.IGNORECASE):
        raise RuntimeError('Nom d’archive Akeeba invalide.')
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        raise RuntimeError('Manifeste sans parties d’archive.')
    names = [item.get('name') for item in files]
    if names.count(archive) != 1 or len(names) != len(set(names)):
        raise RuntimeError('Le manifeste ne décrit pas une seule archive cohérente.')
    for item in files:
        name = item.get('name', '')
        if not name or Path(name).name != name:
            raise RuntimeError('Nom de partie invalide dans le manifeste.')
        path = archive_dir / name
        if not path.is_file() or path.stat().st_size != item.get('size'):
            raise RuntimeError('Taille ou chemin incorrect pour une partie de l’archive.')
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                sha.update(block)
        digest = sha.hexdigest()
        if digest != item.get('sha256'):
            raise RuntimeError('SHA-256 incorrect pour une partie de l’archive.')
    return archive


def archive_sha256(archive_dir):
    manifest = json.loads((archive_dir / 'manifest.json').read_text())
    digest = hashlib.sha256()
    for item in sorted(manifest['files'], key=lambda row: row['name']):
        digest.update(item['name'].encode() + b'\0' + bytes.fromhex(item['sha256']))
    return digest.hexdigest()


def existing_runs_for_archive(digest, exclude=None):
    roots = (RUNTIME,)
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for metadata_path in root.glob('*/run.json'):
            if exclude is not None and metadata_path.parent.resolve() == exclude.resolve():
                continue
            try:
                metadata = json.loads(metadata_path.read_text())
                if metadata.get('archive_sha256') == digest:
                    found.append((metadata_path.parent, metadata.get('stage', 'unknown'), metadata.get('url')))
            except (OSError, ValueError, TypeError):
                continue
    return found


def prepare_and_extract(instance_dir, metadata, archive_dir, archive):
    project = metadata['project']
    runtime = instance_dir / 'www'
    image = run_docker('docker image inspect grimperoots-backup-php83', capture_output=True,
                       text=True, check=False)
    if image.returncode:
        print('Construction de l’image PHP locale requise pour Joomla 6…', flush=True)
        run_docker('docker compose -f j6/docker/build-compose.yaml build web', capture_output=True)
    print(f"Création de l’instance isolée sur le port {metadata['port']}…", flush=True)
    docker_compose(instance_dir, project, 'up -d --wait', capture_output=True)
    metadata['stage'] = 'containers_ready'
    write_run(instance_dir, metadata)
    command = ('docker compose --env-file ' + shlex.quote(str(instance_dir / '.env')) +
               ' -p ' + shlex.quote(project) + ' -f ' + shlex.quote(str(instance_dir / 'compose.yaml')) +
               ' exec -T web php /opt/restore/kickstart.php ' +
               shlex.quote('/opt/deployment/' + archive) + ' /var/www/html/j6 --silent')
    extraction = run_docker(command, capture_output=True)
    (instance_dir / 'extraction.log').write_bytes(extraction.stdout + extraction.stderr)
    if not (runtime / 'j6/index.php').is_file():
        raise RuntimeError('Extraction Kickstart incomplète ; journal conservé dans runtime/ID.')
    metadata['stage'] = 'files_extracted'
    write_run(instance_dir, metadata)
    finish_run(instance_dir, metadata)


def latest_download(root=ROOT / 'backup'):
    candidates = []
    if not root.is_dir():
        raise RuntimeError('Aucun dossier de sauvegardes locales trouvé dans backup/.')
    for directory in root.iterdir():
        manifest_path = directory / 'manifest.json'
        if directory.is_symlink() or not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get('source') != 'ftp-ovh-j6' or not manifest.get('archive'):
                continue
            if long_id(manifest['archive']) != directory.name:
                continue
            downloaded = manifest.get('downloaded_at')
            if downloaded:
                order = downloaded
            else:
                order = directory.name
            candidates.append((order, directory))
        except (OSError, ValueError):
            continue
    if not candidates:
        raise RuntimeError('Aucun téléchargement FTP J6 complet trouvé dans backup/ID/.')
    return max(candidates, key=lambda item: item[0])[1]


def request(opener, url, fields=None, query=None):
    target = url + (('?' + urllib.parse.urlencode(query)) if query else '')
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    req = urllib.request.Request(target, data=data, headers={'User-Agent': 'Grimperoots-local-restore/1.0'})
    with opener.open(req, timeout=180) as response:
        return response.read().decode('utf-8', errors='replace')


def brs_api(opener, url, fields, run_dir):
    body = request(opener, url, fields)
    match = re.search(r'###(.*?)###', body, re.S)
    try:
        result = json.loads(match[1] if match else body)
    except ValueError:
        (run_dir / 'installer-response.html').write_text(body)
        raise RuntimeError('Réponse Akeeba BRS inattendue ; diagnostic conservé dans runtime/ID.') from None
    if isinstance(result, dict) and result.get('error'):
        (run_dir / 'installer-error.json').write_text(json.dumps(result))
        raise RuntimeError('Akeeba BRS a signalé une erreur ; diagnostic conservé dans runtime/ID.')
    return result


def restore_database_and_finish(run_dir, port):
    site = run_dir / 'www/j6'
    definitions_path = site / 'installation/sql/databases.json'
    if not definitions_path.is_file():
        raise RuntimeError('Fichiers d’installation Akeeba absents après extraction.')
    definitions = json.loads(definitions_path.read_text())
    if list(definitions) != ['site.sql'] or definitions['site.sql'].get('prefix') != 'j6_':
        raise RuntimeError('Archive inattendue : une base Joomla avec le préfixe j6_ était attendue.')

    settings = dict(line.split('=', 1) for line in (run_dir / '.env').read_text().splitlines() if '=' in line)
    url = f'http://127.0.0.1:{port}/j6/installation/index.php'
    jar = http.cookiejar.LWPCookieJar(str(run_dir / 'installer-cookies.txt'))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request(opener, url, query={'view': 'main', 'task': 'startover'})
    request(opener, url, query={'view': 'main'})
    if brs_api(opener, url, {'view': 'main', 'task': 'getconfig', 'format': 'json'}, run_dir) is not True:
        raise RuntimeError('Initialisation BRS impossible.')
    brs_api(opener, url, {'view': 'main', 'task': 'detectversion', 'format': 'json'}, run_dir)
    request(opener, url, query={'view': 'main', 'task': 'init'})
    page = request(opener, url, query={'view': 'database', 'substep': 'site'})
    if 'id="dbtype"' not in page:
        raise RuntimeError('Formulaire de base de données BRS introuvable.')

    dbinfo = {
        'dbtype': 'mysqli', 'dbhost': 'db', 'dbport': '3306', 'dbsocket': '',
        'dbuser': 'grimperoots_snapshot', 'dbpass': settings['DB_PASSWORD'],
        'dbname': 'grimperoots_snapshot', 'prefix': 'j6_', 'existing': 'drop',
        'dbencryption': '0', 'foreignkey': '1', 'noautovalue': '1', 'replace': '0',
        'utf8db': '0', 'utf8tables': '0', 'utf8mb4': '1',
        'break_on_failed_create': '1', 'break_on_failed_insert': '1',
        'maxexectime': '5', 'throttle': '100',
    }
    fields = {'view': 'dbrestore', 'task': 'start', 'format': 'json', 'key': 'site'}
    fields.update({f'dbinfo[{key}]': value for key, value in dbinfo.items()})
    result = brs_api(opener, url, fields, run_dir)
    while str(result.get('done')) != '1':
        result = brs_api(opener, url, {'view': 'dbrestore', 'task': 'step', 'format': 'json', 'key': 'site'}, run_dir)
        percent = float(result.get('percent', 0))
        print('[' + '#' * int(percent * .3) + '-' * (30 - int(percent * .3)) + f'] Import MySQL : {percent:.1f} %', flush=True)
        if result.get('errorcount'):
            raise RuntimeError('Erreurs pendant l’import de la base ; instance conservée pour diagnostic.')
        time.sleep(.1)

    form = dict(Page(request(opener, url, query={'view': 'setup'})).inputs)
    for key in ('replacewebconfig', 'removephpini', 'removehtaccess', 'removehtpasswd', 'mailonline'):
        form.pop(key, None)
    form.update(view='setup', task='apply', format='json', livesite='', cookiedomain='', cookiepath='',
                force_ssl='0', mailonline='0', resetsessionoptions='1', resetcacheoptions='1',
                usesitedirs='1', newsecret='1', tmppath='/var/www/html/j6/tmp',
                logspath='/var/www/html/j6/administrator/logs', cache_path='', enableftp='0',
                ftphost='', ftpuser='', ftppass='', ftpdir='', superuseremail='',
                superuserpassword='', superuserpasswordrepeat='')
    brs_api(opener, url, form, run_dir)
    config = site / 'configuration.php'
    text = config.read_text()
    for key, value in {'user': 'grimperoots_snapshot', 'db': 'grimperoots_snapshot', 'dbprefix': 'j6_'}.items():
        if not re.search(r'\$' + key + r"\s*=\s*'" + re.escape(value) + r"'", text):
            raise RuntimeError('Configuration restaurée inattendue : ' + key)
    text = re.sub(r'(public\s+\$mailonline\s*=\s*)[^;]+;', lambda match: match[1] + "'0';", text)
    text = re.sub(r'(public\s+\$robots\s*=\s*)[^;]+;', lambda match: match[1] + "'noindex, nofollow';", text)
    config.write_text(text)
    config.chmod(0o600)
    if brs_api(opener, url, {'view': 'finalise', 'task': 'cleanup', 'format': 'json'}, run_dir) is not True:
        raise RuntimeError('Nettoyage final BRS non confirmé.')
    if (site / 'installation').exists():
        raise RuntimeError('Le dossier installation est encore présent après le nettoyage BRS.')
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive_dir', type=Path, nargs='?',
                        help='dossier backup/ID créé par j6.sh download')
    parser.add_argument('--latest', action='store_true',
                        help='choisir le dernier téléchargement FTP complet sous backup/')
    parser.add_argument('--resume', metavar='ID',
                        help='reprendre une restauration interrompue pour cet identifiant')
    parser.add_argument('--port', type=int, help='port local ; attribué automatiquement par défaut')
    args = parser.parse_args()
    if args.resume:
        if args.latest or args.archive_dir or args.port:
            parser.error('--resume ne se combine pas avec une archive, --latest ou --port')
        instance_rows = []
        if RUNTIME.is_dir():
            for path in RUNTIME.iterdir():
                try:
                    metadata = json.loads((path / 'run.json').read_text())
                    if metadata.get('archive'):
                        instance_rows.append({'archive': metadata['archive'], 'path': path,
                                              'metadata': metadata})
                except (OSError, ValueError, TypeError):
                    continue
        selected = resolve(args.resume, instance_rows, 'instance locale')
        if selected is None:
            raise RuntimeError('Utiliser un identifiant AAAAMMJJ ou AAAAMMJJ-HHMMSS affiché par list.')
        if selected['path'].is_symlink() or selected['path'].resolve().parent != RUNTIME.resolve():
            raise RuntimeError('Instance hors du dossier runtime/ attendu.')
        run_dir = selected['path'].resolve()
        metadata_path = run_dir / 'run.json'
        if not metadata_path.is_file():
            raise RuntimeError('Métadonnées absentes dans runtime/ID.')
        metadata = json.loads(metadata_path.read_text())
        if long_id(metadata.get('archive', '')) != run_dir.name:
            raise RuntimeError('L’identifiant de l’instance ne correspond pas à son archive.')
        if metadata.get('stage') in ('prepared', 'containers_ready'):
            archive_dir = Path(metadata['archive_dir'])
            archive = verify_archive(archive_dir)
            if archive != metadata.get('archive') or archive_sha256(archive_dir) != metadata.get('archive_sha256'):
                raise RuntimeError('L’archive associée à cette instance a changé ; reprise refusée.')
            if existing_runs_for_archive(metadata['archive_sha256'], exclude=run_dir):
                raise RuntimeError('Une restauration de cette archive existe déjà. Examiner les instances affichées par list.')
            if metadata.get('stage') == 'containers_ready' and (run_dir / 'www/j6/index.php').is_file():
                metadata['stage'] = 'files_extracted'
                write_run(run_dir, metadata)
                finish_run(run_dir, metadata)
            else:
                prepare_and_extract(run_dir, metadata, archive_dir, archive)
            return
        if metadata.get('stage') not in ('files_extracted', 'restoring_database', 'verifying'):
            raise RuntimeError('La reprise exige une instance au stade prepared, files_extracted, restoring_database ou verifying.')
        project = metadata['project']
        docker_compose(run_dir, project, 'up -d --wait', capture_output=True)
        finish_run(run_dir, metadata)
        return
    if args.latest == bool(args.archive_dir):
        parser.error('indiquer soit un dossier d’archive, soit --latest')
    archive_dir = (latest_download() if args.latest else args.archive_dir.resolve())
    archive = verify_archive(archive_dir)
    digest = archive_sha256(archive_dir)
    existing = existing_runs_for_archive(digest)
    if existing:
        details = '\n'.join(f'  {path} — {stage}' + (f' — {url}' if url else '')
                            for path, stage, url in existing)
        raise RuntimeError('Cette archive a déjà une instance locale ; aucun nouvel espace disque ne sera créé.\n' +
                           details + '\nUtiliser le lanceur d’une instance existante ; pour une restauration incomplète, utiliser --resume ID.')
    kickstart = KICKSTART / 'kickstart.php'
    metadata_file = KICKSTART / 'download.json'
    if not kickstart.is_file() or not metadata_file.is_file():
        raise RuntimeError('Kickstart Core absent dans vendor/akeeba-kickstart/9.1.0/.')
    source = json.loads(metadata_file.read_text())
    if source.get('version') != '9.1.0' or hashlib.sha256(kickstart.read_bytes()).hexdigest() != source.get('php_sha256'):
        raise RuntimeError('Empreinte SHA-256 de Kickstart Core incorrecte.')
    if args.port is not None and not 1024 <= args.port <= 65535:
        raise RuntimeError('Le port doit être compris entre 1024 et 65535.')
    identifier = long_id(archive)
    if not identifier:
        raise RuntimeError('Impossible de déduire un identifiant AAAAMMJJ-HHMMSS du nom de l’archive.')
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = RUNTIME / identifier
    if run_dir.exists():
        raise RuntimeError(f'Le dossier runtime/{identifier} existe déjà. Aucune extraction lancée.')
    run_dir.mkdir(mode=0o700)
    port = args.port or available_port()
    project, runtime = compose_file(run_dir, archive_dir, port)
    (runtime / 'j6').mkdir(parents=True)
    metadata = {'archive_dir': str(archive_dir), 'archive': archive, 'archive_sha256': digest, 'port': port,
                'url': f'http://localhost:{port}/j6/', 'project': project, 'stage': 'prepared'}
    write_run(run_dir, metadata)
    prepare_and_extract(run_dir, metadata, archive_dir, archive)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterruption : instance, archive et fichiers restent disponibles pour reprise/diagnostic.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            message = 'Docker Compose a échoué ; examiner runtime/ID.'
        else:
            message = str(error)
        print('Échec : ' + message, file=sys.stderr)
        raise SystemExit(1)
