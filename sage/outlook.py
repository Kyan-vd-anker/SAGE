"""Local Outlook calendar connections using Graph OAuth or a published ICS feed."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import re
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from . import config, db

GRAPH = 'https://graph.microsoft.com/v1.0'
SCOPES = 'offline_access User.Read Calendars.ReadWrite Mail.ReadWrite'
ICS_HOSTS = {'outlook.live.com', 'outlook.office365.com', 'outlook.office.com'}
ICS_MAX_BYTES = 2 * 1024 * 1024
_flow_lock = threading.RLock()
_device_flow = None


def _setting(key, default=''):
    rows = db.rows('SELECT value FROM settings WHERE key=?', ('outlook.' + key,))
    return rows[0]['value'] if rows else default


def _save_settings(client_id, tenant):
    for key, value in (('client_id', client_id), ('tenant', tenant)):
        db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('outlook.' + key, value, db.now()))


def _token_path():
    return config.DATA / 'secrets' / 'outlook-token.dat'


def _ics_path():
    return config.DATA / 'secrets' / 'outlook-calendar-url.dat'


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, protect):
    source = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = ([ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)] if protect else
                         [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob), ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)])
    function.restype = ctypes.c_int
    description = None
    if protect:
        ok = function(ctypes.byref(incoming), 'Sage Outlook calendar token', None, None, None, 1, ctypes.byref(outgoing))
    else:
        description = wintypes.LPWSTR()
        ok = function(ctypes.byref(incoming), ctypes.byref(description), None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))
        if description and description.value:
            kernel.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _save_token(token):
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(token, ensure_ascii=False).encode('utf-8')
    stored = _dpapi(raw, True) if os.name == 'nt' else raw
    path.write_bytes(stored)
    if os.name != 'nt':
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _load_token():
    path = _token_path()
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if os.name == 'nt':
        raw = _dpapi(raw, False)
    return json.loads(raw.decode('utf-8'))


def _save_ics_url(url):
    path = _ics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = _dpapi(url.encode('utf-8'), True) if os.name == 'nt' else url.encode('utf-8')
    path.write_bytes(stored)
    if os.name != 'nt':
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _load_ics_url():
    path = _ics_path()
    if not path.is_file():
        return ''
    raw = path.read_bytes()
    if os.name == 'nt':
        raw = _dpapi(raw, False)
    return raw.decode('utf-8')


def disconnect():
    global _device_flow
    with _flow_lock:
        _device_flow = None
    _token_path().unlink(missing_ok=True)
    return {'connected': False}


def connect_ics(url):
    url = str(url).strip()
    _validate_ics_url(url)
    _save_ics_url(url)
    return {'ics_connected': True, 'source': 'ics'}


def disconnect_ics():
    _ics_path().unlink(missing_ok=True)
    return {'ics_connected': False}


def _validate_ics_url(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname and
                 parsed.hostname.lower() in ICS_HOSTS and
                 parsed.username is None and parsed.password is None and
                 parsed.port in (None, 443) and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Use the HTTPS ICS publishing link copied from Outlook (outlook.live.com or outlook.office365.com).')


class _OutlookRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_ics_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _unescape_ics(value):
    return value.replace(r'\n', '\n').replace(r'\N', '\n').replace(r'\,', ',').replace(r'\;', ';').replace(r'\\', '\\')


def _parse_ics_datetime(params, value):
    if params.get('VALUE', '').upper() == 'DATE' or re.fullmatch(r'\d{8}', value):
        parsed_date = datetime.strptime(value, '%Y%m%d').date()
        return datetime.combine(parsed_date, datetime_time.min).astimezone(), True
    if value.endswith('Z'):
        parsed = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        return parsed, False
    else:
        parsed = datetime.strptime(value, '%Y%m%dT%H%M%S')
        tzid = params.get('TZID')
        if tzid:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            windows_tz = {
                'UTC': 'UTC', 'GMT Standard Time': 'Europe/London',
                'W. Europe Standard Time': 'Europe/Berlin', 'Romance Standard Time': 'Europe/Paris',
                'Eastern Standard Time': 'America/New_York', 'Central Standard Time': 'America/Chicago',
                'Mountain Standard Time': 'America/Denver', 'Pacific Standard Time': 'America/Los_Angeles',
                'India Standard Time': 'Asia/Kolkata', 'Tokyo Standard Time': 'Asia/Tokyo',
                'AUS Eastern Standard Time': 'Australia/Sydney',
            }
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(windows_tz.get(tzid, tzid)))
            except ZoneInfoNotFoundError as exc:
                try:
                    from dateutil.tz import gettz
                except ImportError:
                    raise ValueError(f'Install python-dateutil to read this Outlook time zone: {tzid}') from exc
                parsed_zone = gettz(tzid) or gettz(windows_tz.get(tzid, ''))
                if parsed_zone is None:
                    raise ValueError(f'Unsupported calendar time zone in Outlook feed: {tzid}') from exc
                parsed = parsed.replace(tzinfo=parsed_zone)
        else:
            parsed = parsed.astimezone()
    return parsed, False


def _matches_weekday_rule(current_date, weekday, ordinal):
    if current_date.weekday() != weekday:
        return False
    if ordinal is None:
        return True
    month_end = (date(current_date.year, current_date.month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    ordinal_in_month = (current_date.day - 1) // 7 + 1 if ordinal > 0 else (month_end.day - current_date.day) // 7 + 1
    return ordinal_in_month == abs(ordinal)


def _recurrence_occurrences(event_start, event_end, rule, start, end, exclusions=()):
    if not rule:
        return [(event_start, event_end)] if event_start < end and event_end > start else []
    parts = dict(part.split('=', 1) for part in rule.split(';') if '=' in part)
    frequency = parts.get('FREQ', '').upper()
    if frequency not in {'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'}:
        raise ValueError(f'Unsupported Outlook calendar recurrence frequency: {frequency or "unknown"}')
    interval = max(1, int(parts.get('INTERVAL', '1')))
    count_limit = int(parts['COUNT']) if 'COUNT' in parts else None
    until = _parse_ics_datetime({}, parts['UNTIL'])[0] if 'UNTIL' in parts else None
    month_days = [int(value) for value in parts.get('BYMONTHDAY', '').split(',') if value]
    months = [int(value) for value in parts.get('BYMONTH', '').split(',') if value]
    weekday_numbers = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}
    weekdays = []
    for value in parts.get('BYDAY', '').split(','):
        if value:
            match = re.fullmatch(r'([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)', value)
            if not match:
                raise ValueError(f'Unsupported Outlook calendar weekday rule: {value}')
            weekdays.append((weekday_numbers[match.group(2)], int(match.group(1)) if match.group(1) else None))
    duration = event_end - event_start
    start_date = event_start.date()
    last_date = end.date()
    excluded = set()
    for params, value in exclusions:
        for item in value.split(','):
            excluded.add(_parse_ics_datetime(params, item)[0])
    found = []
    occurrence_count = 0
    current_date = start_date
    scanned_days = 0
    while current_date <= last_date:
        scanned_days += 1
        if scanned_days > 100000:
            raise ValueError('Outlook calendar recurrence starts too far in the past to expand safely.')
        day_delta = (current_date - start_date).days
        month_delta = (current_date.year - start_date.year) * 12 + current_date.month - start_date.month
        year_delta = current_date.year - start_date.year
        matches = current_date >= start_date
        if frequency == 'DAILY':
            matches = matches and day_delta % interval == 0
        elif frequency == 'WEEKLY':
            week_delta = (day_delta + start_date.weekday()) // 7
            by_weekday = [day for day, ordinal in weekdays] if weekdays else [start_date.weekday()]
            matches = matches and week_delta % interval == 0 and current_date.weekday() in by_weekday
        elif frequency == 'MONTHLY':
            matches = matches and month_delta % interval == 0
        elif frequency == 'YEARLY':
            matches = matches and year_delta % interval == 0
            months = months or [event_start.month]
            matches = matches and current_date.month in months
        if months and frequency != 'YEARLY':
            matches = matches and current_date.month in months
        if month_days:
            month_end = (date(current_date.year, current_date.month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            valid_month_days = {day if day > 0 else month_end.day + day + 1 for day in month_days}
            matches = matches and current_date.day in valid_month_days
        elif frequency == 'MONTHLY' and not weekdays and not month_days:
            matches = matches and current_date.day == event_start.day
        elif frequency == 'YEARLY' and not weekdays and not month_days:
            matches = matches and current_date.day == event_start.day
        if weekdays and frequency in {'DAILY', 'MONTHLY', 'YEARLY'}:
            matches = matches and any(_matches_weekday_rule(current_date, day, ordinal) for day, ordinal in weekdays)
        if matches:
            occurrence = datetime.combine(current_date, event_start.timetz()).replace(tzinfo=event_start.tzinfo)
            if until and occurrence > until:
                break
            occurrence_count += 1
            if count_limit is not None and occurrence_count > count_limit:
                break
            occurrence_end = occurrence + duration
            if occurrence not in excluded and occurrence < end and occurrence_end > start:
                found.append((occurrence, occurrence_end))
        current_date += timedelta(days=1)
    return found


def _parse_ics_events(content, start, end):
    start_utc, end_utc = _utc(start), _utc(end)
    lines = []
    for line in content.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line.startswith((' ', '\t')) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events = []
    current = None
    for line in lines:
        if line == 'BEGIN:VEVENT':
            current = {}
            continue
        if line == 'END:VEVENT':
            if current is not None:
                starts = current.get('DTSTART')
                ends = current.get('DTEND')
                if starts and ends:
                    event_start, all_day = _parse_ics_datetime(*starts)
                    event_end, _ = _parse_ics_datetime(*ends)
                    recurrence_rule = current.get('RRULE', ({}, ''))[1]
                    occurrence_ranges = _recurrence_occurrences(
                        event_start, event_end, recurrence_rule, start_utc, end_utc,
                        current.get('EXDATE', []),
                    )
                    status = current.get('STATUS', ({}, ''))[1].upper()
                    transparency = current.get('TRANSP', ({}, ''))[1].upper()
                    for occurrence_start, occurrence_end in occurrence_ranges:
                        events.append({
                            'id': _unescape_ics(current.get('UID', ({}, ''))[1]),
                            'subject': _unescape_ics(current.get('SUMMARY', ({}, '(No title)'))[1]) or '(No title)',
                            'start': occurrence_start.astimezone().isoformat(timespec='minutes'),
                            'end': occurrence_end.astimezone().isoformat(timespec='minutes'),
                            'location': _unescape_ics(current.get('LOCATION', ({}, ''))[1]),
                            'show_as': 'free' if transparency == 'TRANSPARENT' else 'busy',
                            'is_all_day': all_day,
                            'cancelled': status == 'CANCELLED',
                            'web_link': _unescape_ics(current.get('URL', ({}, ''))[1]),
                        })
            current = None
            continue
        if current is None or ':' not in line:
            continue
        header, value = line.split(':', 1)
        parts = header.split(';')
        name = parts[0].upper()
        params = {}
        for part in parts[1:]:
            if '=' in part:
                key, param_value = part.split('=', 1)
                params[key.upper()] = param_value.strip('"')
        if name == 'EXDATE':
            current.setdefault(name, []).append((params, value))
        else:
            current[name] = (params, value)
    return sorted(events, key=lambda event: datetime.fromisoformat(event['start']))


def _ics_events_between(start, end):
    url = _load_ics_url()
    if not url:
        raise PermissionError('Outlook is not connected. Connect it from Sage’s Daily briefing page.')
    _validate_ics_url(url)
    request = Request(url, headers={'Accept': 'text/calendar', 'User-Agent': 'Sage calendar reader'})
    opener = build_opener(_OutlookRedirectHandler())
    try:
        with opener.open(request, timeout=20) as response:
            content = response.read(ICS_MAX_BYTES + 1)
    except HTTPError as exc:
        raise ConnectionError(f'Outlook calendar feed returned HTTP {exc.code}. Check the published link in Sage.') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(f'Could not fetch the Outlook calendar feed: {exc}') from exc
    if len(content) > ICS_MAX_BYTES:
        raise ValueError('Outlook calendar feed is larger than Sage’s 2 MB limit.')
    return _parse_ics_events(content.decode('utf-8-sig'), start, end)


def _post_form(url, fields):
    request = Request(url, data=urlencode(fields).encode('utf-8'), headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError(f'Microsoft sign-in failed with HTTP {exc.code}') from exc
    except (URLError, TimeoutError) as exc:
        raise ConnectionError(f'Could not reach Microsoft sign-in: {exc}') from exc


def begin_device_flow(client_id, tenant='common'):
    global _device_flow
    client_id = str(client_id or _setting('client_id')).strip()
    tenant = str(tenant or _setting('tenant') or 'common').strip()
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', client_id):
        raise ValueError('Enter the Application (client) ID from your Microsoft app registration.')
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,100}', tenant):
        raise ValueError('Tenant must be common, consumers, organizations, or a tenant ID/domain.')
    _save_settings(client_id, tenant)
    url = f'https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode'
    result = _post_form(url, {'client_id': client_id, 'scope': SCOPES})
    if result.get('error'):
        raise ValueError(result.get('error_description') or result['error'])
    with _flow_lock:
        _device_flow = {
            'device_code': result['device_code'], 'client_id': client_id, 'tenant': tenant,
            'interval': max(5, int(result.get('interval', 5))),
            'expires_at': time.time() + int(result.get('expires_in', 900)),
            'next_poll': time.time(), 'user_code': result.get('user_code', ''),
            'verification_uri': result.get('verification_uri', 'https://microsoft.com/devicelogin'),
            'message': result.get('message', 'Sign in with your Microsoft account to connect Outlook.'),
        }
        return _public_flow(_device_flow)


def _public_flow(flow):
    return {'pending': True, 'user_code': flow['user_code'], 'verification_uri': flow['verification_uri'],
            'message': flow['message'], 'interval': flow['interval'], 'expires_in': max(0, int(flow['expires_at'] - time.time()))}


def pending_flow():
    with _flow_lock:
        return _public_flow(_device_flow) if _device_flow else None


def poll_device_flow():
    global _device_flow
    with _flow_lock:
        flow = _device_flow
        if not flow:
            return {'pending': False, 'connected': bool(_load_token())}
        if time.time() >= flow['expires_at']:
            _device_flow = None
            return {'pending': False, 'connected': False, 'error': 'The sign-in code expired. Start the connection again.'}
        if time.time() < flow['next_poll']:
            return _public_flow(flow)
        result = _post_form(f"https://login.microsoftonline.com/{flow['tenant']}/oauth2/v2.0/token", {
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'client_id': flow['client_id'], 'device_code': flow['device_code'],
        })
        error = result.get('error')
        if error == 'authorization_pending':
            flow['next_poll'] = time.time() + flow['interval']
            return _public_flow(flow)
        if error == 'slow_down':
            flow['interval'] += 5
            flow['next_poll'] = time.time() + flow['interval']
            return _public_flow(flow)
        if error:
            _device_flow = None
            detail = result.get('error_description') or error
            return {'pending': False, 'connected': False, 'error': detail[:500]}
        token = dict(result)
        token['client_id'] = flow['client_id']
        token['tenant'] = flow['tenant']
        token['expires_at'] = time.time() + int(result.get('expires_in', 3600))
        token['account'] = ''
        _save_token(token)
        _device_flow = None
    try:
        token = _load_token()
        profile = _graph_request('/me?$select=displayName,mail,userPrincipalName', token=token)
        token['account'] = profile.get('mail') or profile.get('userPrincipalName') or profile.get('displayName', '')
        _save_token(token)
    except Exception:
        pass
    return {'pending': False, 'connected': True, 'account': (_load_token() or {}).get('account', '')}


def _refresh(token):
    # Refresh only permissions this token already has. Adding Mail.ReadWrite needs a
    # visible device-code sign-in, not a silent scope expansion or a broken refresh.
    refresh_scope = str(token.get('scope') or 'offline_access User.Read Calendars.ReadWrite')
    refreshed = _post_form(f"https://login.microsoftonline.com/{token['tenant']}/oauth2/v2.0/token", {
        'grant_type': 'refresh_token', 'client_id': token['client_id'],
        'refresh_token': token['refresh_token'], 'scope': refresh_scope,
    })
    if refreshed.get('error'):
        disconnect()
        raise PermissionError('Outlook authorization expired. Reconnect your calendar in Sage.')
    updated = dict(token)
    updated.update(refreshed)
    updated['expires_at'] = time.time() + int(refreshed.get('expires_in', 3600))
    _save_token(updated)
    return updated


def _graph_request(path_or_url, method='GET', body=None, token=None, retry=True):
    token = token or _load_token()
    if not token:
        raise PermissionError('Outlook is not connected. Connect it from Sage’s Daily briefing page.')
    if float(token.get('expires_at', 0)) < time.time() + 120:
        if not token.get('refresh_token'):
            raise PermissionError('Outlook authorization expired. Reconnect your calendar in Sage.')
        token = _refresh(token)
    url = path_or_url if path_or_url.startswith('https://') else GRAPH + path_or_url
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    headers = {'Authorization': 'Bearer ' + token['access_token'], 'Accept': 'application/json', 'Prefer': 'outlook.timezone="UTC"'}
    if '$search=' in url or '%24search=' in url.casefold():
        headers['ConsistencyLevel'] = 'eventual'
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode('utf-8')) if response.status != 204 else {}
    except HTTPError as exc:
        if exc.code == 401 and retry and token.get('refresh_token'):
            return _graph_request(path_or_url, method, body, _refresh(token), retry=False)
        try:
            error = json.loads(exc.read().decode('utf-8')).get('error', {})
            message = error.get('message') or error.get('code') or str(error)
        except (ValueError, UnicodeDecodeError):
            message = f'HTTP {exc.code}'
        if exc.code == 401:
            disconnect()
            raise PermissionError('Outlook authorization expired. Reconnect your calendar in Sage.') from exc
        if exc.code == 403:
            raise PermissionError(f'Microsoft Graph denied this request: {message[:400]}. If this is an email action, reconnect Outlook and grant Mail.ReadWrite.') from exc
        raise ValueError(f'Microsoft Graph returned {exc.code}: {message[:500]}') from exc
    except (URLError, TimeoutError) as exc:
        raise ConnectionError(f'Could not reach Microsoft Graph: {exc}') from exc


def status():
    token = _load_token()
    ics_connected = bool(_load_ics_url())
    graph_connected = bool(token)
    source = 'ics' if ics_connected else 'graph' if graph_connected else ''
    granted = set(str((token or {}).get('scope', '')).split())
    mail_connected = bool({'Mail.ReadWrite', 'Mail.Read'} & granted)
    mail_drafts_connected = 'Mail.ReadWrite' in granted
    return {'connected': graph_connected, 'graph_connected': graph_connected, 'mail_connected': mail_connected,
            'mail_drafts_connected': mail_drafts_connected, 'ics_connected': ics_connected,
            'calendar_connected': graph_connected or ics_connected, 'source': source,
            'account': (token or {}).get('account', ''), 'tenant': _setting('tenant', 'common'),
            'client_id_configured': bool(_setting('client_id')), 'token_storage': 'Windows DPAPI' if os.name == 'nt' else 'local file permissions'}


def _utc(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Use an ISO date/time, such as 2026-10-10T09:00:00+02:00.') from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _event_time(value):
    raw = value.get('dateTime', '')
    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().isoformat(timespec='minutes')


def events_between(start, end):
    if _load_ics_url():
        return _ics_events_between(start, end)
    return _graph_events_between(start, end)


def _graph_events_between(start, end):
    start_utc, end_utc = _utc(start), _utc(end)
    query = urlencode({'startDateTime': start_utc.isoformat(timespec='seconds').replace('+00:00', 'Z'),
                       'endDateTime': end_utc.isoformat(timespec='seconds').replace('+00:00', 'Z'),
                       '$orderby': 'start/dateTime', '$top': '100',
                       '$select': 'id,subject,start,end,location,showAs,isAllDay,isCancelled,webLink'})
    result = []
    next_url = GRAPH + '/me/calendarView?' + query
    pages = 0
    while next_url and pages < 5:
        data = _graph_request(next_url)
        for event in data.get('value', []):
            result.append({'id': event.get('id', ''), 'subject': event.get('subject') or '(No title)',
                           'start': _event_time(event.get('start') or {}), 'end': _event_time(event.get('end') or {}),
                           'location': (event.get('location') or {}).get('displayName', ''),
                           'show_as': event.get('showAs', ''), 'is_all_day': bool(event.get('isAllDay')),
                           'cancelled': bool(event.get('isCancelled')), 'web_link': event.get('webLink', '')})
        next_url = data.get('@odata.nextLink')
        pages += 1
    return result


def upcoming(days=7):
    days = max(1, min(90, int(days)))
    now = datetime.now().astimezone()
    return events_between(now, now + timedelta(days=days))


def search_events(query, days=90):
    terms = [term.casefold() for term in re.findall(r'[^\W_]+', str(query or ''), re.UNICODE) if len(term) > 1]
    if not terms:
        raise ValueError('Give a word or phrase to find in calendar event titles or locations.')
    days = max(1, min(365, int(days)))
    now = datetime.now().astimezone()
    events = events_between(now, now + timedelta(days=days))
    matches = [event for event in events if all(term in (event.get('subject', '') + ' ' + event.get('location', '')).casefold() for term in terms)]
    return {'query': str(query)[:160], 'days': days, 'count': len(matches), 'events': matches[:50]}


def _mail_message(item):
    sender = (item.get('from') or {}).get('emailAddress') or {}
    return {'id': item.get('id', ''), 'subject': str(item.get('subject') or '(No subject)')[:300],
            'from': str(sender.get('name') or sender.get('address') or 'Unknown sender')[:240],
            'received': item.get('receivedDateTime', ''), 'is_read': bool(item.get('isRead')),
            'preview': str(item.get('bodyPreview') or '')[:900], 'web_link': item.get('webLink', '')}


def _mail_list(path):
    if not _load_token():
        raise PermissionError('Connect Outlook with Microsoft Graph and grant Mail.ReadWrite in Sage’s briefing page to use email tools.')
    result = _graph_request(path)
    return [_mail_message(item) for item in result.get('value', []) if isinstance(item, dict)]


def search_emails(query, limit=10):
    query = re.sub(r'\s+', ' ', str(query or '')).strip()[:180]
    if not query:
        raise ValueError('Give a sender, subject, or phrase to search for.')
    limit = max(1, min(25, int(limit)))
    # Graph $search supports words/phrases over Outlook message fields. Escape the quoted expression.
    expression = '"' + query.replace('\\', '\\\\').replace('"', '\\"') + '"'
    params = urlencode({'$search': expression, '$top': str(limit), '$select': 'id,subject,from,receivedDateTime,isRead,bodyPreview,webLink'})
    return {'query': query, 'messages': _mail_list('/me/messages?' + params)}


def inbox_brief(limit=12):
    limit = max(1, min(25, int(limit)))
    params = urlencode({'$filter': 'isRead eq false', '$orderby': 'receivedDateTime desc', '$top': str(limit),
                        '$select': 'id,subject,from,receivedDateTime,isRead,bodyPreview,webLink'})
    messages = _mail_list('/me/mailFolders/inbox/messages?' + params)
    return {'count': len(messages), 'unread_messages': messages}


def latest_emails(limit=5):
    limit = max(1, min(15, int(limit)))
    params = urlencode({'$orderby': 'receivedDateTime desc', '$top': str(limit),
                        '$select': 'id,subject,from,receivedDateTime,isRead,bodyPreview,webLink'})
    messages = _mail_list('/me/mailFolders/inbox/messages?' + params)
    return {'count': len(messages), 'messages': messages}


def draft_email_reply(message_id, reply_text):
    message_id = str(message_id or '').strip()
    reply_text = str(reply_text or '').strip()
    if not message_id or len(message_id) > 1000:
        raise ValueError('Choose a message from Sage’s email search results.')
    if not reply_text:
        raise ValueError('The reply draft cannot be empty.')
    if len(reply_text) > 12000:
        raise ValueError('Keep the reply draft under 12,000 characters.')
    if not _load_token():
        raise PermissionError('Connect Outlook with Microsoft Graph and grant Mail.ReadWrite in Sage’s briefing page to create reply drafts.')
    encoded_id = quote(message_id, safe='')
    created = _graph_request(f'/me/messages/{encoded_id}/createReply', method='POST', body={})
    draft_id = str(created.get('id') or '')
    if not draft_id:
        raise ValueError('Microsoft Graph did not return the reply draft id; Sage did not send an email.')
    draft_body = created.get('body') if isinstance(created.get('body'), dict) else {}
    content_type = draft_body.get('contentType') if draft_body.get('contentType') in ('Text', 'HTML') else 'Text'
    original = str(draft_body.get('content') or '')
    if content_type == 'HTML':
        import html
        content = '<p>' + '<br>'.join(html.escape(line) for line in reply_text.splitlines()) + '</p><br>' + original
    else:
        content = reply_text + ('\n\n' + original if original else '')
    encoded_draft_id = quote(draft_id, safe='')
    saved = _graph_request(f'/me/messages/{encoded_draft_id}', method='PATCH', body={'body': {'contentType': content_type, 'content': content}})
    return {'draft_created': True, 'sent': False, 'message_id': draft_id,
            'subject': str(saved.get('subject') or created.get('subject') or '(Reply draft)'),
            'message': 'Reply saved in Outlook Drafts. It has not been sent.'}


def explicit_email_draft_request(message):
    text = str(message or '').strip()
    return bool(re.search(r"(?i)^\s*(?:(?:sage|hey sage)[,:]?\s*)?(?:(?:please\s+)?(?:can|could) you\s+)?(?:please\s+)?(?:draft|write|prepare)\s+(?:me\s+)?(?:a\s+)?(?:reply|response)\b|^\s*(?:(?:please\s+)?(?:can|could) you\s+)?(?:please\s+)?(?:reply|respond)\s+to\s+(?:that|this|the|my|message|email|an?\b)", text))


def create_event(title, start, end, location='', notes=''):
    title = str(title).strip()[:200]
    location = str(location).strip()[:300]
    notes = str(notes).strip()[:2000]
    if not title:
        raise ValueError('Calendar event title is required.')
    start_utc, end_utc = _utc(start), _utc(end)
    if end_utc <= start_utc:
        raise ValueError('Calendar event end must be after its start.')
    if not _load_token():
        raise PermissionError('Creating events requires the optional Microsoft Graph connection; an ICS feed is read-only.')
    conflicts = _graph_events_between(start_utc - timedelta(hours=12), end_utc + timedelta(hours=12))
    busy = [event for event in conflicts if not event['cancelled'] and event['show_as'] != 'free'
            and _utc(event['start']) < end_utc and _utc(event['end']) > start_utc]
    if busy:
        return {'created': False, 'conflicts': [{'subject': event['subject'], 'start': event['start'], 'end': event['end']} for event in busy[:8]]}
    payload = {'subject': title,
               'start': {'dateTime': start_utc.replace(tzinfo=None).isoformat(timespec='seconds'), 'timeZone': 'UTC'},
               'end': {'dateTime': end_utc.replace(tzinfo=None).isoformat(timespec='seconds'), 'timeZone': 'UTC'},
               'isOnlineMeeting': False}
    if location:
        payload['location'] = {'displayName': location}
    if notes:
        payload['body'] = {'contentType': 'text', 'content': notes}
    event = _graph_request('/me/events', method='POST', body=payload)
    return {'created': True, 'id': event.get('id', ''), 'subject': event.get('subject', title),
            'start': _event_time(event.get('start') or payload['start']), 'end': _event_time(event.get('end') or payload['end']),
            'web_link': event.get('webLink', '')}
