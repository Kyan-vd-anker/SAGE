"""Bounded search of user-selected local folders for readable text files."""
import os
import re
from pathlib import Path

from . import config

_TEXT_SUFFIXES = {'.txt', '.md', '.rst', '.csv', '.tsv', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg',
                  '.py', '.js', '.ts', '.html', '.css', '.xml', '.log', '.sql', '.sh', '.bat', '.ps1', '.gcode'}
_SKIP_DIRS = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__', '.cache', '.local',
              'appdata', 'program files', 'windows', 'system volume information'}
_SENSITIVE_NAMES = re.compile(r'(?i)(^\.env(?:\.|$)|password|credential|secret|token|id_rsa|private.key|\.pem$|\.pfx$|\.p12$)')
_MAX_FILE_BYTES = 512_000
_MAX_FILES_SCANNED = 12000


def _roots():
    raw = str(config.FILE_SEARCH_ROOTS or '').strip()
    if raw:
        # Accept a semicolon-delimited list on Windows and the native path separator elsewhere.
        parts = raw.split(';') if ';' in raw else raw.split(os.pathsep)
        roots = [Path(item.strip()).expanduser() for item in parts if item.strip()]
    else:
        home = Path.home()
        roots = [home / folder for folder in ('Documents', 'Desktop', 'Downloads')]
    valid = []
    for root in roots:
        try:
            resolved = root.resolve(strict=True)
            if resolved.is_dir() and resolved not in valid:
                valid.append(resolved)
        except (OSError, RuntimeError):
            continue
    return valid


def search_files(query, limit=20):
    query = re.sub(r'\s+', ' ', str(query or '')).strip()
    terms = [part.casefold() for part in re.findall(r'[^\W_]+', query, re.UNICODE) if len(part) > 1]
    if not terms:
        raise ValueError('Give a filename, phrase, or a few distinctive words to search for.')
    roots = _roots()
    if not roots:
        return {'query': query[:180], 'roots': [], 'results': [], 'message': 'No searchable folders found. Configure SAGE_FILE_SEARCH_ROOTS for folders Sage may search.'}
    limit = max(1, min(50, int(limit)))
    results = []
    scanned = 0
    truncated = False
    for root in roots:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not name.startswith('.') and name.casefold() not in _SKIP_DIRS
                       and not (current_path / name).is_symlink()]
            for filename in files:
                scanned += 1
                if scanned > _MAX_FILES_SCANNED:
                    truncated = True
                    break
                path = current_path / filename
                if filename.startswith('.') or _SENSITIVE_NAMES.search(filename):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_file() or not resolved.is_relative_to(root) or resolved.suffix.lower() not in _TEXT_SUFFIXES:
                        continue
                    if resolved.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    with resolved.open('rb') as handle:
                        raw = handle.read(_MAX_FILE_BYTES + 1)
                    if len(raw) > _MAX_FILE_BYTES:
                        continue
                    text = raw.decode('utf-8-sig')
                except (OSError, UnicodeError, RuntimeError):
                    continue
                folded_name = filename.casefold()
                folded_text = text.casefold()
                name_hits = sum(term in folded_name for term in terms)
                text_hits = sum(folded_text.count(term) for term in terms)
                if not name_hits and not text_hits:
                    continue
                line_num = None
                snippet = ''
                for index, line in enumerate(text.splitlines(), 1):
                    if any(term in line.casefold() for term in terms):
                        line_num = index
                        snippet = re.sub(r'\s+', ' ', line).strip()[:360]
                        break
                score = name_hits * 8 + sum(min(5, folded_text.count(term)) for term in terms)
                results.append({'path': str(resolved), 'name': filename, 'line': line_num, 'snippet': snippet, 'score': score})
            if truncated:
                break
        if truncated:
            break
    results.sort(key=lambda item: (-item['score'], item['path'].casefold()))
    return {'query': query[:180], 'roots': [str(root) for root in roots], 'results': results[:limit],
            'matches_found': len(results), 'files_scanned': min(scanned, _MAX_FILES_SCANNED), 'scan_limited': truncated}
