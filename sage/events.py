"""Sage's event bus.

Anything that happens (a reminder fires, a background task finishes, a plan changes ...) is published here once.
Events are stored in the database so clients can catch up, and waiting clients (browser, wall display, phone)
are woken immediately.

    events.publish('timer.done', 'Pasta timer', 'Time is up', level='alert')
    events.wait_for(since=last_id, timeout=25)      # long-poll for new events
"""
import json
import threading
import time

from . import db

_cond = threading.Condition()
_latest = 0
LEVELS = ('info', 'success', 'warning', 'alert')
_last_prune = 0.0


def _load_latest():
    global _latest
    row = db.rows('SELECT COALESCE(MAX(id),0) AS id FROM events')
    _latest = row[0]['id'] if row else 0


def publish(kind, title, body='', level='info', data=None):
    """Store an event and wake everyone waiting for one. Returns the event id."""
    global _latest
    if level not in LEVELS:
        level = 'info'
    event_id = db.execute(
        'INSERT INTO events(kind,title,body,level,data_json,created_at) VALUES(?,?,?,?,?,?)',
        (str(kind)[:60], str(title)[:200], str(body)[:2000], level, json.dumps(data or {}, ensure_ascii=False, default=str), db.now()))
    with _cond:
        _latest = max(_latest, event_id)
        _cond.notify_all()
    _maybe_prune()
    return event_id


def _row(row):
    try:
        data = json.loads(row.get('data_json') or '{}')
    except ValueError:
        data = {}
    return {'id': row['id'], 'kind': row['kind'], 'title': row['title'], 'body': row['body'],
            'level': row['level'], 'data': data, 'created_at': row['created_at']}


def recent(since=0, limit=50, kinds=None):
    """Events newer than `since`, oldest first (so a client can replay them in order)."""
    limit = max(1, min(int(limit), 200))
    if since:
        rows = db.rows('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (int(since), limit))
    else:
        rows = list(reversed(db.rows('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))))
    items = [_row(r) for r in rows]
    if kinds:
        items = [e for e in items if any(e['kind'] == k or e['kind'].startswith(k + '.') for k in kinds)]
    return items


def cursor():
    """Id of the newest event (0 if none). Clients start listening from here."""
    if not _latest:
        _load_latest()
    return _latest


def wait_for(since, timeout=25.0, limit=100):
    """Block until there is an event newer than `since` (or the timeout passes), then return the new events."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    since = int(since)
    if not _latest:
        _load_latest()
    with _cond:
        while _latest <= since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _cond.wait(min(remaining, 5.0))
    return recent(since, limit) if _latest > since else []


def _maybe_prune():
    """Keep the table small: drop events older than 21 days, at most once an hour."""
    global _last_prune
    if time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat(timespec='seconds')
        db.execute('DELETE FROM events WHERE created_at<?', (cutoff,))
    except Exception:
        pass
