"""Helpers shared by the Grimperoots Akeeba download and restore tools."""

import hashlib
import json
import re
import shutil
import time
from html.parser import HTMLParser


class BackupError(Exception):
    pass


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.hidden = {}
        self.options = {}
        self.script = None
        self.buffer = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.inputs[attrs['name']] = attrs.get('value', '')
            if attrs.get('type') == 'hidden':
                self.hidden[attrs['name']] = attrs.get('value', '')
        if tag == 'script' and attrs.get('type') == 'application/json':
            self.script = True
            self.buffer = []

    def handle_data(self, data):
        if self.script:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script:
            try:
                parsed = json.loads(''.join(self.buffer))
                if isinstance(parsed, dict):
                    self.options.update(parsed)
            except ValueError:
                pass
            self.script = None


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def download(ftp, parts, output):
    required = sum(max(0, part['size'] - ((output / (part['name'] + '.partial')).stat().st_size
                  if (output / (part['name'] + '.partial')).exists() else 0))
                   for part in parts if not (output / part['name']).exists())
    if shutil.disk_usage(output).free < required + 256 * 1024**2:
        raise BackupError('Espace disque local insuffisant pour toutes les parties.')
    total = sum(part['size'] for part in parts)
    received = 0
    last_progress = 0.0

    def progress(block, stream):
        nonlocal received, last_progress
        stream.write(block)
        received += len(block)
        now = time.monotonic()
        if now - last_progress >= 5:
            fraction = received / total
            filled = min(30, int(fraction * 30))
            print('[' + '#' * filled + '-' * (30 - filled) +
                  f'] FTP {fraction:.1%} — {received / 1e9:.2f}/{total / 1e9:.2f} Go', flush=True)
            last_progress = now

    completed = []
    for part in parts:
        target = output / part['name']
        partial = output / (part['name'] + '.partial')
        if target.exists() and target.stat().st_size != part['size']:
            raise BackupError('Fichier local existant de taille différente : ' + part['name'])
        if not target.exists():
            offset = partial.stat().st_size if partial.exists() else 0
            received += offset
            if offset > part['size']:
                raise BackupError('Fichier partiel trop grand : ' + part['name'])
            print(f"Téléchargement {part['name']} ({part['size'] / 1024**3:.2f} Gio), reprise à {offset} octets", flush=True)
            with partial.open('ab') as stream:
                partial.chmod(0o600)
                if offset < part['size']:
                    ftp.retrbinary('RETR ' + part['remote'], lambda block: progress(block, stream),
                                   blocksize=1024 * 1024, rest=offset if offset else None)
            if partial.stat().st_size != part['size'] or ftp.size(part['remote']) != part['size']:
                raise BackupError('Téléchargement incomplet ou archive distante modifiée : ' + part['name'])
            partial.replace(target)
        else:
            received += part['size']
        target.chmod(0o600)
        completed.append({'name': part['name'], 'size': part['size'], 'sha256': sha256(target)})
    print('[##############################] FTP 100 % ; tailles vérifiées et SHA256 calculés.', flush=True)
    return completed
