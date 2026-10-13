"""Identifiants stables fondés sur la date portée par les archives Akeeba."""

from collections import defaultdict
from datetime import datetime
import re

STAMP = re.compile(r'(?<!\d)(\d{8})-(\d{6})(?!\d)')
IDENTIFIER = re.compile(r'\d{8}(?:-\d{6})?\Z')


def long_id(archive_name):
    matches = STAMP.findall(archive_name)
    if not matches:
        return None
    day, clock = matches[-1]
    try:
        datetime.strptime(day + clock, '%Y%m%d%H%M%S')
    except ValueError:
        return None
    return day + '-' + clock


def display_ids(archive_names):
    by_day = defaultdict(set)
    for name in archive_names:
        identifier = long_id(name)
        if identifier:
            by_day[identifier[:8]].add(identifier)
    return {identifier: identifier[:8] if len(by_day[identifier[:8]]) == 1 else identifier
            for identifiers in by_day.values() for identifier in identifiers}


def resolve(identifier, rows, kind, allow_same_name=False):
    """Return one row, refusing short and full timestamp collisions."""
    if not IDENTIFIER.fullmatch(identifier):
        return None
    matches = [row for row in rows if (stamp := long_id(row['archive'])) and
               (identifier == stamp or identifier == stamp[:8])]
    if not matches:
        raise ValueError(f'Aucun {kind} pour l’identifiant {identifier}.')
    stamps = {long_id(row['archive']) for row in matches}
    if len(stamps) > 1:
        raise ValueError(f'Identifiant {identifier} ambigu ; utiliser AAAAMMJJ-HHMMSS.')
    names = {row['archive'] for row in matches}
    if len(names) > 1:
        raise ValueError(f'Horodatage {identifier} partagé par plusieurs archives distinctes ; choix impossible.')
    if len(matches) > 1 and not allow_same_name:
        raise ValueError(f'Identifiant {identifier} partagé par plusieurs {kind} ; examiner ./j6.sh list.')
    return matches[0]
