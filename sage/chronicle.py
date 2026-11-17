"""The event log: what happened, written down as episodic memories.

Sage used to know what Kyan said. This module also records what he did, so "what did I get done this week?", "when did the print fail?"
and "what changed in the ROV tether since Friday?" have answers.

Sources
  * database triggers queue every new workshop note, decision, failure lesson, measurement run, test result and completed task
    (the triggers live in db.py, so every code path is covered: tools, the web UI, voice, Telegram)
  * poll_printer(): OctoPrint print outcomes (finished, cancelled, failed), when OctoPrint is configured
  * poll_repos(): new git commits in SAGE_REPO_ROOTS (a burst of commits becomes one memory)

Everything lands as an `episodic` memory (kind 'event', source 'event: <kind>', date-stamped, importance 2-4, never merged with a
similar older event). Episodic memories are only pulled into a prompt when the question is about the past, so the log costs the
normal chat path nothing. Low-importance events fade after 120 days (memory.maintain), the daily digest keeps the gist.

tick() is cheap and is called by the scheduler loop.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db, memory

_lock = threading.Lock()
_last = {'repos': 0.0, 'printer': 0.0, 'path': None}
REPO_INTERVAL = 15 * 60
PRINTER_INTERVAL = 45
COMMIT_BURST = 5


def _stamp(value=None):
    """Local, human date-time for the start of the memory text."""
    try:
        moment = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return datetime.now().strftime('%Y-%m-%d %H:%M')


def _clip(text, size):
    return ' '.join(str(text or '').split())[:size]


def record(kind, text, key=None, project_id=None, importance=2, when=None):
    """Write one event. Returns the memory id, or None when it was a repeat, empty, private or switched off."""
    if not config.CHRONICLE_ENABLED:
        return None
    text = _clip(text, 480)
    if not text:
        return None
    key = key or hashlib.sha1(f'{kind}|{text}'.encode('utf-8')).hexdigest()[:20]
    if db.rows('SELECT 1 FROM chronicle_seen WHERE key=?', (key,)):
        return None
    mid = memory.save(f'{_stamp(when)}: {text}', 'event', f'event: {kind}', 0.9, importance=importance, quiet=True,
                      tier='episodic', project_id=project_id, distinct=True)
    db.execute('INSERT OR IGNORE INTO chronicle_seen(key,memory_id,created_at) VALUES(?,?,?)', (key, mid, db.now()))
    return mid


# ------------------------------------------------------------------ rows queued by the database triggers

def _project(project_id):
    if not project_id:
        return ''
    rows = db.rows('SELECT name FROM projects WHERE id=?', (project_id,))
    return f" ({rows[0]['name']})" if rows else ''


def _describe(source, row_id):
    """-> (text, project_id, importance) for one queued row, or None when the row no longer exists."""
    if source == 'workshop':
        rows = db.rows('SELECT project_id,note,measurements FROM workshop_entries WHERE id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        extra = f" Measurements: {_clip(r['measurements'], 140)}." if r['measurements'] else ''
        return f"Workshop note{_project(r['project_id'])}: {_clip(r['note'], 260)}.{extra}", r['project_id'], 2
    if source == 'decision':
        rows = db.rows('SELECT project_id,title,decision,rationale FROM decision_log WHERE id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        why = f" Why: {_clip(r['rationale'], 160)}." if r['rationale'] else ''
        return f"Decision{_project(r['project_id'])}: {_clip(r['title'], 80)}, {_clip(r['decision'], 200)}.{why}", r['project_id'], 3
    if source == 'failure':
        rows = db.rows('SELECT project_id,title,symptom,cause,fix,lesson FROM failure_lessons WHERE id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        parts = [f"Failure{_project(r['project_id'])}: {_clip(r['title'], 80)}. Symptom: {_clip(r['symptom'], 140)}."]
        for label, key in (('Cause', 'cause'), ('Fix', 'fix'), ('Lesson', 'lesson')):
            if r[key]:
                parts.append(f'{label}: {_clip(r[key], 120)}.')
        return ' '.join(parts), r['project_id'], 4
    if source == 'measurement':
        rows = db.rows('SELECT r.project_id,r.notes,t.title FROM measurement_runs r JOIN measurement_templates t ON t.id=r.template_id WHERE r.id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        values = db.rows('SELECT field_name,value,unit,target FROM measurement_values WHERE run_id=? ORDER BY id LIMIT 8', (row_id,))
        readings = ', '.join(f"{v['field_name']} {v['value']}{(' ' + v['unit']) if v['unit'] else ''}" + (f" (target {v['target']})" if v['target'] else '') for v in values)
        notes = f" {_clip(r['notes'], 120)}." if r['notes'] else ''
        return f"Measurements{_project(r['project_id'])}, {_clip(r['title'], 80)}: {readings or 'no values recorded'}.{notes}", r['project_id'], 3
    if source == 'test':
        rows = db.rows('SELECT r.outcome,r.notes,r.measurements,d.title,d.project_id FROM engineering_doc_results r JOIN engineering_docs d ON d.id=r.doc_id WHERE r.id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        notes = f" {_clip(r['notes'], 140)}." if r['notes'] else ''
        data = f" Measurements: {_clip(r['measurements'], 120)}." if r['measurements'] else ''
        failed = bool(re.search(r'(?i)fail|error|not ok', r['outcome'] or ''))
        return f"Test result{_project(r['project_id'])}, {_clip(r['title'], 80)}: {_clip(r['outcome'], 40)}.{notes}{data}", r['project_id'], 4 if failed else 3
    if source == 'task':
        rows = db.rows('SELECT project_id,title FROM tasks WHERE id=?', (row_id,))
        if not rows:
            return None
        r = rows[0]
        return f"Finished task{_project(r['project_id'])}: {_clip(r['title'], 160)}.", r['project_id'], 2
    return None


def drain(limit=200):
    """Turn queued trigger rows into memories. -> number written."""
    written = 0
    for item in db.rows('SELECT id,source,row_id,created_at FROM chronicle_queue ORDER BY id LIMIT ?', (int(limit),)):
        try:
            described = _describe(item['source'], item['row_id'])
            if described:
                text, project_id, importance = described
                if record(item['source'], text, key=f"{item['source']}:{item['row_id']}", project_id=project_id,
                          importance=importance, when=item['created_at']):
                    written += 1
        except Exception:
            pass                                               # one bad row must never block the queue
        db.execute('DELETE FROM chronicle_queue WHERE id=?', (item['id'],))
    return written


# ------------------------------------------------------------------ the 3D printer

def _printer_snapshot():
    from . import octoprint
    data = octoprint._request('/api/job')
    progress = data.get('progress') if isinstance(data.get('progress'), dict) else {}
    job = data.get('job') if isinstance(data.get('job'), dict) else {}
    file = job.get('file') if isinstance(job.get('file'), dict) else {}
    state = str(data.get('state') or '')
    return {'state': state, 'file': str(file.get('name') or file.get('display') or ''),
            'completion': float(progress.get('completion') or 0), 'seconds': int(progress.get('printTime') or 0)}


def _in_print(state):
    return state.lower().startswith(('print', 'paus', 'resum'))


def poll_printer(snapshot=None):
    """Compare OctoPrint's job state with the last poll and log a print that just ended. -> memory id or None."""
    if not config.CHRONICLE_ENABLED:
        return None
    try:
        from . import octoprint
        if snapshot is None:
            if not octoprint.configured():
                return None
            snapshot = _printer_snapshot()
    except Exception:
        return None
    try:
        before = json.loads(memory._setting('printer_watch', '') or '{}')
    except (ValueError, TypeError):
        before = {}
    now_printing = _in_print(snapshot['state'])
    result = None
    if before.get('printing') and not now_printing:
        done = float(before.get('completion') or 0)
        name = before.get('file') or 'a print'
        minutes = max(1, round(int(before.get('seconds') or 0) / 60))
        state = snapshot['state'].lower()
        if 'error' in state or 'offline' in state:
            text, importance = f'Print failed: {name} stopped with the printer in an error state at {done:.0f}% after {minutes} min.', 4
        elif done >= 99:
            text, importance = f'Print finished: {name} ({minutes} min).', 3
        else:
            text, importance = f'Print stopped early: {name} ended at {done:.0f}% after {minutes} min (cancelled or aborted).', 4
        result = record('printer', text, key=f"printer:{name}:{before.get('started', '')}", importance=importance)
    if now_printing:
        started = before.get('started') if before.get('printing') and before.get('file') == snapshot['file'] else db.now()
        memory._set_setting('printer_watch', json.dumps({'printing': True, 'file': snapshot['file'], 'completion': snapshot['completion'],
                                                          'seconds': snapshot['seconds'], 'started': started}))
    elif before.get('printing'):
        memory._set_setting('printer_watch', json.dumps({'printing': False}))
    return result


# ------------------------------------------------------------------ git commits

def repo_roots():
    found = []
    for part in re.split(r'[;\n]', config.REPO_ROOTS or ''):
        part = part.strip().strip('"')
        if part and os.path.isdir(os.path.join(part, '.git')) and part not in found:
            found.append(part)
    return found


def _git_log(repo, since):
    out = subprocess.run(['git', '-C', repo, 'log', f'--since={since}', '--no-merges', '--max-count=40', '--pretty=format:%H%x1f%aI%x1f%s'],
                         capture_output=True, text=True, timeout=20, encoding='utf-8', errors='replace')
    if out.returncode != 0:
        return []
    commits = []
    for line in out.stdout.splitlines():
        parts = line.split('\x1f')
        if len(parts) == 3:
            commits.append({'sha': parts[0], 'when': parts[1], 'subject': parts[2]})
    return commits


def poll_repos(commits_for=None):
    """Log new commits. `commits_for(repo)` can be supplied by tests. -> number of memories written."""
    if not config.CHRONICLE_ENABLED:
        return 0
    written = 0
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds')
    for repo in repo_roots():
        try:
            commits = commits_for(repo) if commits_for else _git_log(repo, since)
        except (OSError, subprocess.SubprocessError):
            return written                                     # git is not installed or the repo is unreadable: try again later
        name = os.path.basename(repo.rstrip('\\/')) or repo
        new = [c for c in commits if not db.rows('SELECT 1 FROM chronicle_seen WHERE key=?', (f"commit:{name}:{c['sha']}",))]
        if not new:
            continue
        new.reverse()                                          # oldest first
        if len(new) > COMMIT_BURST:
            subjects = '; '.join(_clip(c['subject'], 70) for c in new[-6:])
            mid = record('commit', f'{len(new)} commits in {name}, latest: {subjects}.', key=f"commit:{name}:{new[-1]['sha']}", importance=2, when=new[-1]['when'])
            for c in new:
                db.execute('INSERT OR IGNORE INTO chronicle_seen(key,memory_id,created_at) VALUES(?,?,?)', (f"commit:{name}:{c['sha']}", mid, db.now()))
            written += 1 if mid else 0
            continue
        for c in new:
            if record('commit', f"Commit in {name}: {_clip(c['subject'], 160)} ({c['sha'][:7]}).", key=f"commit:{name}:{c['sha']}", importance=2, when=c['when']):
                written += 1
    return written


# ------------------------------------------------------------------ scheduler entry point

def tick():
    """Called about once a minute by the scheduler. Always cheap: drain the queue, poll the printer and the repos on their own clocks."""
    if not config.CHRONICLE_ENABLED:
        return
    with _lock:
        if _last['path'] != str(config.DB_PATH):
            _last.update(repos=0.0, printer=0.0, path=str(config.DB_PATH))
        now = time.time()
        drain()
        if now - _last['printer'] >= PRINTER_INTERVAL:
            _last['printer'] = now
            poll_printer()
        if now - _last['repos'] >= REPO_INTERVAL:
            _last['repos'] = now
            poll_repos()
