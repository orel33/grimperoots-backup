#!/usr/bin/env python3
"""Check a restored Joomla 6 snapshot through its local HTTP endpoint."""

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import urllib.parse
import urllib.request

from backup_ids import long_id, resolve

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'img' and attrs.get('src'):
            self.images.append(attrs['src'])


class LocalRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            raise RuntimeError('Redirection hors de l’instance locale : ' + newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify_run(instance_dir, port):
    base = f'http://localhost:{port}/j6/'
    opener = urllib.request.build_opener(LocalRedirects())
    results = []

    def fetch(path, kind):
        url = urllib.parse.urljoin(base, path)
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base).netloc:
            raise RuntimeError('Lien hors de l’instance locale : ' + url)
        with opener.open(url, timeout=30) as response:
            body = response.read()
            content_type = response.headers.get('Content-Type', '')
            if response.status != 200 or not body:
                raise RuntimeError('Réponse HTTP incorrecte : ' + url)
            if kind == 'image' and not content_type.startswith('image/'):
                raise RuntimeError('Média non reconnu : ' + url)
            if kind == 'home' and 'noindex' not in response.headers.get('X-Robots-Tag', ''):
                raise RuntimeError('En-tête de non-indexation absent.')
            results.append({'url': url, 'status': response.status, 'bytes': len(body),
                            'content_type': content_type})
            print(f'{kind}: HTTP {response.status} — {urllib.parse.urlsplit(url).path}', flush=True)
            return body

    home = Links()
    home.feed(fetch(base, 'home').decode('utf-8', errors='replace'))
    articles = [path for path in dict.fromkeys(home.links)
                if path.startswith('/j6/les-crs/') and '?' not in path and '#' not in path]
    if len(articles) < 3:
        raise RuntimeError('Accueil trop incomplet pour vérifier trois liens SEF d’articles.')
    for path in articles[:3]:
        article = Links()
        article.feed(fetch(path, 'article').decode('utf-8', errors='replace'))
        for src in list(dict.fromkeys(article.images))[:2]:
            fetch(urllib.parse.urljoin(urllib.parse.urljoin(base, path), src), 'image')
    for path in ('les-crs', 'categories', 'all-tags'):
        fetch(path, 'navigation')
    admin = fetch('administrator/', 'admin').decode('utf-8', errors='replace')
    if 'name="passwd"' not in admin:
        raise RuntimeError('Formulaire de connexion Joomla non trouvé.')
    target = instance_dir / 'verification.json'
    target.write_text(json.dumps({'url': base, 'http': results}, indent=2, ensure_ascii=False) + '\n')
    target.chmod(0o600)
    print('Rapport : ' + str(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('identifier', help='identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    args = parser.parse_args()
    rows = []
    if RUNTIME.is_dir():
        for path in RUNTIME.iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                metadata = json.loads((path / 'run.json').read_text())
                if metadata.get('archive') and long_id(metadata['archive']) == path.name:
                    rows.append({'archive': metadata['archive'], 'path': path, 'metadata': metadata})
            except (OSError, ValueError, TypeError):
                continue
    selected = resolve(args.identifier, rows, 'instance locale')
    if selected is None:
        parser.error('utiliser un identifiant AAAAMMJJ[-HHMMSS] affiché par list')
    instance_dir = selected['path']
    metadata = selected['metadata']
    verify_run(instance_dir, int(metadata['port']))


if __name__ == '__main__':
    main()
