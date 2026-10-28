"""Small, bounded OctoPrint REST client for reading its G-code library and starting an explicitly requested file."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

from . import config

_ALLOWED_GCODE = {'.gcode', '.gco', '.gc'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured():
    return bool(str(config.OCTOPRINT_URL).strip() and str(config.OCTOPRINT_API_KEY).strip())


def _base_url():
    base = str(config.OCTOPRINT_URL or '').strip().rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError('Configure SAGE_OCTOPRINT_URL as the OctoPrint http(s) base address, without a username, password, query, or fragment.')
    key = str(config.OCTOPRINT_API_KEY or '').strip()
    if not key or '\r' in key or '\n' in key:
        raise RuntimeError('OctoPrint is not configured. Set SAGE_OCTOPRINT_API_KEY in Sage’s private service environment.')
    return base, key


def _request(path, method='GET', payload=None):
    base, key = _base_url()
    url = base + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'X-Api-Key': key, 'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=8) as response:
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read(600).decode('utf-8', 'replace').strip()
        except OSError:
            pass
        raise RuntimeError(f'OctoPrint returned HTTP {exc.code}' + (f': {detail}' if detail else '.')) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f'Could not reach OctoPrint: {exc}') from exc
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('OctoPrint returned invalid JSON.') from exc
    if not isinstance(result, dict):
        raise RuntimeError('OctoPrint returned an unexpected response.')
    return result


def _storage(value=None):
    storage = str(value or config.OCTOPRINT_STORAGE or 'local').strip().lower()
    if storage not in ('local', 'sdcard'):
        raise ValueError('Choose OctoPrint storage local or sdcard.')
    return storage


def status():
    if not configured():
        return {'configured': False, 'connected': False, 'message': 'Set SAGE_OCTOPRINT_URL and SAGE_OCTOPRINT_API_KEY to connect OctoPrint.'}
    data = _request('/api/printer')
    state = data.get('state') if isinstance(data.get('state'), dict) else {}
    flags = state.get('flags') if isinstance(state.get('flags'), dict) else {}
    temps = data.get('temperature') if isinstance(data.get('temperature'), dict) else {}
    def reading(name):
        item = temps.get(name) if isinstance(temps.get(name), dict) else {}
        return {key: item[key] for key in ('actual', 'target') if isinstance(item.get(key), (int, float))}
    return {
        'configured': True,
        'connected': True,
        'state': str(state.get('text', 'unknown'))[:80],
        'operational': bool(flags.get('operational')),
        'printing': bool(flags.get('printing')),
        'paused': bool(flags.get('paused')),
        'tool0': reading('tool0'),
        'bed': reading('bed'),
    }


def _safe_path(value):
    path = str(value or '').strip().replace('\\', '/')
    if not path or path.startswith('/') or '\x00' in path or re.match(r'^[A-Za-z]:', path):
        raise ValueError('Use a relative file path from the OctoPrint library.')
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('The OctoPrint library path is invalid.')
    return '/'.join(parts)


def _walk_files(items, storage, output):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get('path') or '').strip()
        children = item.get('children')
        if isinstance(children, list):
            _walk_files(children, storage, output)
            continue
        if not path:
            continue
        try:
            path = _safe_path(path)
        except ValueError:
            continue
        name = str(item.get('name') or PurePosixPath(path).name)[:180]
        kind = str(item.get('type') or item.get('typePath') or '').lower()
        if PurePosixPath(name).suffix.lower() not in _ALLOWED_GCODE and kind != 'machinecode':
            continue
        if PurePosixPath(name).suffix.lower() not in _ALLOWED_GCODE:
            continue
        analysis = item.get('gcodeAnalysis') if isinstance(item.get('gcodeAnalysis'), dict) else {}
        size = item.get('size')
        size = int(size) if isinstance(size, (int, float)) and size >= 0 else None
        duration = analysis.get('estimatedPrintTime')
        duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
        output.append({'name': name, 'path': path, 'storage': storage, 'size_bytes': size, 'estimated_seconds': duration})


def list_files(storage=None, limit=100):
    storage = _storage(storage)
    if not configured():
        raise RuntimeError('OctoPrint is not configured. See OCTOPRINT.md.')
    limit = max(1, min(200, int(limit)))
    query = urllib.parse.urlencode({'recursive': 'true'})
    result = _request(f'/api/files/{storage}?{query}')
    files = []
    _walk_files(result.get('files'), storage, files)
    files.sort(key=lambda item: item['path'].casefold())
    return {'storage': storage, 'count': len(files), 'files': files[:limit], 'truncated': len(files) > limit}


def explicit_print_request(message, path):
    text = str(message or '').strip()
    command = re.search(r"(?i)^\s*(?:(?:sage|hey sage)[,:]?\s*)?(?:please\s+)?(?:print|start\s+(?:printing|a\s+print(?:\s+of)?)|send\s+.+?\s+to\s+(?:the\s+)?printer)\b|^\s*(?:can|could)\s+you\s+(?:please\s+)?(?:print|start\s+printing)\b", text)
    if not command:
        return False
    try:
        safe = _safe_path(path)
    except ValueError:
        return False
    name = PurePosixPath(safe).name
    return bool(name and re.search(r'(?i)(?<![\w])' + re.escape(name) + r'(?![\w])', text))


def start_print(path, storage=None):
    storage = _storage(storage)
    path = _safe_path(path)
    if PurePosixPath(path).suffix.lower() not in _ALLOWED_GCODE:
        raise ValueError('Sage can start existing G-code files only. It will not slice or run STL/3MF models.')
    encoded = '/'.join(urllib.parse.quote(part, safe='') for part in path.split('/'))
    matching = _request(f'/api/files/{storage}/{encoded}')
    name = str(matching.get('name') or PurePosixPath(path).name)[:180]
    if (matching.get('type') != 'machinecode' and PurePosixPath(name).suffix.lower() not in _ALLOWED_GCODE) or PurePosixPath(name).suffix.lower() not in _ALLOWED_GCODE:
        raise ValueError('That exact path is not an existing G-code file in the selected OctoPrint library. List the library and choose an existing G-code file.')
    analysis = matching.get('gcodeAnalysis') if isinstance(matching.get('gcodeAnalysis'), dict) else {}
    duration = analysis.get('estimatedPrintTime')
    duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
    state = status()
    if not state.get('operational'):
        raise RuntimeError(f'The printer is not operational (state: {state.get("state", "unknown")}). No print was started.')
    if state.get('printing') or state.get('paused'):
        raise RuntimeError('The printer already has an active or paused job. No new print was started.')
    result = _request(f'/api/files/{storage}/{encoded}', method='POST', payload={'command': 'select', 'print': True})
    return {
        'status': 'print_command_accepted',
        'file': path,
        'storage': storage,
        'estimated_seconds': duration,
        'message': 'OctoPrint accepted the command to select and start this existing G-code file.',
        'response': result,
    }
