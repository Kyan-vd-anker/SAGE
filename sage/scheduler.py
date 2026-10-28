"""Sage's clock: fires reminders and timers every second and runs recurring schedules.

Schedule specs (stored in a canonical form, entered in plain words):
    every:30            every 30 minutes            ("every 30 minutes", "every 2 hours")
    daily@07:30         every day at 07:30          ("every day at 7:30")
    weekdays@07:30      Monday to Friday            ("every weekday at 7:30")
    weekly:0@09:00      every Monday (0=Mon..6=Sun) ("every monday at 9")
    monthly:1@08:00     on the 1st of the month     ("monthly on the 1st at 8")
"""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db, events, reminders

CANONICAL = re.compile(r'^(?:every:\d+|daily@\d{2}:\d{2}|weekdays@\d{2}:\d{2}|weekly:[0-6]@\d{2}:\d{2}|monthly:\d{1,2}@\d{2}:\d{2})$')
_thread = None
_stop = threading.Event()


# ------------------------------------------------------------------ specs

def normalize(text):
    """Turn 'every monday at 9' (or an already canonical spec) into the canonical form."""
    text = str(text).strip()
    if CANONICAL.match(text):
        return text
    now = reminders.local_now()
    repeat, rest = reminders.extract_repeat(text)
    if not repeat:
        raise ValueError('I could not read that schedule. Try "every day at 7:30", "every weekday at 8", "every monday at 9" or "every 2 hours".')
    if repeat.startswith('every:'):
        return repeat
    found = reminders.extract_time(rest, now, 'am')
    when = found[0] if found else None
    hhmm = when.strftime('%H:%M') if when else '09:00'
    if repeat == 'daily':
        return f'daily@{hhmm}'
    if repeat == 'weekdays':
        return f'weekdays@{hhmm}'
    if repeat.startswith('weekly:'):
        return f'{repeat}@{hhmm}'
    if repeat == 'weekly':
        return f'weekly:{(when or now).weekday()}@{hhmm}'
    if repeat == 'monthly':
        day = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', rest)
        return f'monthly:{min(int(day.group(1)), 28) if day else 1}@{hhmm}'
    raise ValueError('Unsupported schedule')


def describe(spec):
    kind, _, rest = spec.partition('@')
    base, _, arg = kind.partition(':')
    at = rest
    if base == 'every':
        minutes = int(arg)
        return f'every {minutes // 60} h' if minutes % 60 == 0 and minutes >= 60 else f'every {minutes} min'
    if base == 'daily':
        return f'every day at {at}'
    if base == 'weekdays':
        return f'every weekday at {at}'
    if base == 'weekly':
        return f'every {reminders.WEEKDAY_NAMES[int(arg)]} at {at}'
    if base == 'monthly':
        return f'day {arg} of every month at {at}'
    return spec


def next_run(spec, after=None):
    after = (after or reminders.local_now()).astimezone()
    kind, _, at = spec.partition('@')
    base, _, arg = kind.partition(':')
    if base == 'every':
        return after + timedelta(minutes=max(1, int(arg)))
    hour, minute = (int(x) for x in at.split(':'))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(400):
        ok = candidate > after
        if ok and base == 'weekdays':
            ok = candidate.weekday() < 5
        elif ok and base == 'weekly':
            ok = candidate.weekday() == int(arg)
        elif ok and base == 'monthly':
            ok = candidate.day == int(arg)
        if ok:
            return candidate
        candidate += timedelta(days=1)
    raise ValueError('Could not compute the next run')


# ------------------------------------------------------------------ actions

def _user_name():
    for row in db.rows("SELECT content FROM memories WHERE status='active' AND content LIKE '%name is %' ORDER BY importance DESC, updated_at DESC LIMIT 3"):
        match = re.search(r"name is ([A-Za-zÀ-ÿ'\-]+)", row['content'])
        if match:
            return match.group(1)
    return ''


def _post_message(text):
    db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)', ('assistant', text, db.now()))


def build_brief(now=None):
    now = now or reminders.local_now()
    name = _user_name()
    hour = now.hour
    hello = 'Good morning' if hour < 12 else 'Good afternoon' if hour < 18 else 'Good evening'
    lines = [f"{hello}{', ' + name if name else ''}. Here is your brief for {now.strftime('%A %d %B')}."]
    try:
        from . import outlook
        today = [e for e in outlook.upcoming(1) if not e.get('cancelled')]
        if today:
            lines.append('\nCalendar:')
            for e in today[:6]:
                start = datetime.fromisoformat(e['start']).astimezone()
                lines.append(f"- {'all day' if e.get('is_all_day') else start.strftime('%H:%M')}  {e['subject']}")
        else:
            lines.append('\nCalendar: nothing scheduled.')
    except Exception:
        pass
    due = [r for r in reminders.active() if reminders.from_iso(r['due_at']).date() == now.date()]
    if due:
        lines.append('\nReminders and timers today:')
        for r in due[:6]:
            lines.append(f"- {reminders.from_iso(r['due_at']).astimezone().strftime('%H:%M')}  {r['title']}")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=14)).isoformat(timespec='seconds')
    finished = db.rows("SELECT title,result_summary FROM tasks WHERE status='done' AND finished_at>=? ORDER BY finished_at DESC LIMIT 5", (cutoff,))
    if finished:
        lines.append('\nSage finished since yesterday:')
        for t in finished:
            lines.append(f"- {t['title']}" + (f": {t['result_summary'][:100]}" if t['result_summary'] else ''))
    from . import planner
    view = planner.plan_view()
    if view['queue']:
        lines.append(f"\nSage's queue today (about {view['total_eta_minutes']} min of work):")
        for i in view['queue'][:3]:
            lines.append(f"{i['position']}. {i['title']} ({i['project'] or 'no project'})")
    if view['mine']:
        lines.append('\nYour tasks: ' + '; '.join(i['title'] for i in view['mine'][:4]))
    blocked = view['blocked'][:3]
    if blocked:
        lines.append('\nBlocked: ' + '; '.join(f"{i['title']} ({i['blocked_reason'] or 'needs a look'})" for i in blocked))
    overdue = db.rows("SELECT title FROM tasks WHERE status!='done' AND due_date IS NOT NULL AND due_date!='' AND due_date<? LIMIT 4", (now.date().isoformat(),))
    if overdue:
        lines.append('\nOverdue: ' + '; '.join(t['title'] for t in overdue))
    return '\n'.join(lines)


def post_morning_brief(source='schedule'):
    text = build_brief()
    _post_message(text)
    events.publish('brief.ready', 'Your morning brief is ready', text.split('\n')[0], 'success', {'text': text, 'source': source})
    return text


def action_morning_brief(payload):
    post_morning_brief()
    return 'Brief posted'


def action_awake_brief():
    awake_at = db.now()
    events.publish('user.awake', 'Wake signal received', 'iPhone Shortcut', 'info', {'awake_at': awake_at})
    return {'awake_at': awake_at, 'answer': post_morning_brief('awake')}


def action_weekly_review(payload):
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds')
    done = db.rows("SELECT t.title,p.name AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id WHERE t.status='done' AND t.finished_at>=? ORDER BY t.finished_at", (week,))
    blocked = db.rows("SELECT title,blocked_reason FROM tasks WHERE status='blocked' LIMIT 6")
    stalled = db.rows("SELECT p.name FROM projects p WHERE p.status='active' AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.finished_at>=?) AND EXISTS (SELECT 1 FROM tasks t WHERE t.project_id=p.id AND t.status!='done') LIMIT 5", (week,))
    lines = ['Weekly review.']
    lines.append(f"\nFinished this week ({len(done)}):" if done else '\nNothing was finished this week.')
    lines += [f"- {d['title']} ({d['project'] or 'no project'})" for d in done[:10]]
    if blocked:
        lines.append('\nBlocked: ' + '; '.join(f"{b['title']} ({b['blocked_reason'] or 'needs a look'})" for b in blocked))
    if stalled:
        lines.append('\nNo progress this week: ' + ', '.join(s['name'] for s in stalled))
    from . import planner
    lines.append('\n' + planner.describe())
    text = '\n'.join(lines)
    _post_message(text)
    events.publish('brief.ready', 'Your weekly review is ready', f'{len(done)} tasks finished this week', 'success', {'text': text})
    return 'Review posted'


def action_backup_db(payload):
    folder = config.DATA / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"sage-{datetime.now().strftime('%Y%m%d-%H%M')}.db"
    source = sqlite3.connect(config.DB_PATH)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    for old in sorted(folder.glob('sage-*.db'))[:-14]:
        try:
            old.unlink()
        except OSError:
            pass
    return f'Backup saved: {target.name}'


def action_memory_maintenance(payload):
    from . import memory
    return memory.maintain()


def action_daily_digest(payload):
    from . import jobs
    jobs.create('Daily digest', 'Summarise the day', 'digest')
    return 'Digest queued'


def action_prompt(payload):
    from . import jobs
    prompt = str(payload.get('prompt', '')).strip()
    if not prompt:
        raise ValueError('This schedule has no prompt')
    jobs.create('Scheduled: ' + str(payload.get('title') or prompt[:60]), prompt, 'scheduled_prompt', json.dumps({'title': payload.get('title') or prompt[:60]}))
    return 'Prompt queued'


ACTIONS = {
    'morning_brief': (action_morning_brief, 'Post a brief with your calendar, reminders, Sage\'s overnight work and the plan'),
    'weekly_review': (action_weekly_review, 'Summarise the week and what is stuck'),
    'backup_db': (action_backup_db, 'Save a copy of the Sage database (keeps the last 14)'),
    'memory_maintenance': (action_memory_maintenance, 'Merge duplicate memories and fill embeddings'),
    'daily_digest': (action_daily_digest, 'Summarise today\'s conversations into memory'),
    'prompt': (action_prompt, 'Ask Sage something on a schedule and post the answer'),
}

DEFAULTS = [
    ('Morning brief', 'weekdays@07:30', 'morning_brief', {'catchup_minutes': 240}),
    ('Weekly review', 'weekly:6@18:00', 'weekly_review', {'catchup_minutes': 600}),
    ('Nightly backup', 'daily@03:00', 'backup_db', {'catchup_minutes': 100000}),
    ('Memory upkeep', 'daily@03:30', 'memory_maintenance', {'catchup_minutes': 100000}),
    ('Daily digest', 'daily@23:30', 'daily_digest', {'catchup_minutes': 300}),
]


def run_prompt_job(job):
    """Background job for the 'prompt' action: answer with the local model and post the result to the chat."""
    from . import agent, llm, memory
    try:
        context = json.loads(job.get('result') or '{}')
    except (ValueError, TypeError):
        context = {}
    mems = '\n'.join('- ' + m['content'] for m in memory.relevant(job['prompt'], limit=6, track=False))
    answer = llm.chat([{'role': 'system', 'content': agent.SYSTEM + ('\nRelevant saved memory:\n' + mems if mems else '') + '\nThis is a scheduled request: answer concisely.'},
                       {'role': 'user', 'content': job['prompt']}], timeout=240, max_tokens=700, background=True).strip()
    title = context.get('title') or job['title']
    _post_message(f'{title}\n\n{answer}')
    events.publish('brief.ready', f'Scheduled answer: {title}', answer[:200], 'success', {'text': answer})
    db.execute('UPDATE jobs SET status=?,result=?,checkpoint=?,updated_at=? WHERE id=?', ('completed', answer, 'Posted to chat', db.now(), job['id']))


# ------------------------------------------------------------------ storage + API

def seed_defaults():
    for name, spec, action, payload in DEFAULTS:
        if not db.rows('SELECT id FROM schedules WHERE name=? AND builtin=1', (name,)):
            db.execute('INSERT INTO schedules(name,spec,action,payload_json,enabled,next_run,builtin,created_at) VALUES(?,?,?,?,1,?,1,?)',
                       (name, spec, action, json.dumps(payload), reminders.to_utc(next_run(spec)), db.now()))
    for row in db.rows("SELECT id,spec FROM schedules WHERE enabled=1 AND next_run=''"):
        db.execute('UPDATE schedules SET next_run=? WHERE id=?', (reminders.to_utc(next_run(row['spec'])), row['id']))


def _public(row):
    try:
        payload = json.loads(row['payload_json'] or '{}')
    except ValueError:
        payload = {}
    return {'id': row['id'], 'name': row['name'], 'spec': row['spec'], 'when': describe(row['spec']), 'action': row['action'],
            'payload': payload, 'enabled': bool(row['enabled']), 'last_run': row['last_run'], 'next_run': row['next_run'],
            'last_status': row['last_status'], 'builtin': bool(row['builtin']),
            'description': ACTIONS.get(row['action'], (None, ''))[1]}


def list_schedules():
    return [_public(r) for r in db.rows('SELECT * FROM schedules ORDER BY builtin DESC, id')]


def create_schedule(name, when_text, action='prompt', payload=None):
    name = ' '.join(str(name).split())[:120]
    if not name:
        raise ValueError('Give the schedule a name')
    if action not in ACTIONS:
        raise ValueError('Unknown action. Choose one of: ' + ', '.join(ACTIONS))
    spec = normalize(when_text)
    payload = payload or {}
    if action == 'prompt' and not str(payload.get('prompt', '')).strip():
        raise ValueError('A prompt schedule needs a prompt')
    sid = db.execute('INSERT INTO schedules(name,spec,action,payload_json,enabled,next_run,builtin,created_at) VALUES(?,?,?,?,1,?,0,?)',
                     (name, spec, action, json.dumps(payload, ensure_ascii=False), reminders.to_utc(next_run(spec)), db.now()))
    events.publish('schedule.created', f'Scheduled: {name}', describe(spec), 'info', {'id': sid})
    return _public(db.rows('SELECT * FROM schedules WHERE id=?', (sid,))[0])


def update_schedule(schedule_id, **fields):
    rows = db.rows('SELECT * FROM schedules WHERE id=?', (int(schedule_id),))
    if not rows:
        raise ValueError('Schedule not found')
    sets, args = [], []
    if fields.get('name'):
        sets.append('name=?')
        args.append(' '.join(str(fields['name']).split())[:120])
    spec = rows[0]['spec']
    if fields.get('when'):
        spec = normalize(fields['when'])
        sets += ['spec=?', 'next_run=?']
        args += [spec, reminders.to_utc(next_run(spec))]
    if fields.get('enabled') is not None:
        sets.append('enabled=?')
        args.append(1 if fields['enabled'] else 0)
        if fields['enabled']:
            sets.append('next_run=?')
            args.append(reminders.to_utc(next_run(spec)))
    if fields.get('payload') is not None:
        sets.append('payload_json=?')
        args.append(json.dumps(fields['payload'], ensure_ascii=False))
    if not sets:
        raise ValueError('Nothing to change')
    db.execute(f"UPDATE schedules SET {','.join(sets)} WHERE id=?", (*args, int(schedule_id)))
    return _public(db.rows('SELECT * FROM schedules WHERE id=?', (int(schedule_id),))[0])


def delete_schedule(schedule_id):
    rows = db.rows('SELECT * FROM schedules WHERE id=?', (int(schedule_id),))
    if not rows:
        raise ValueError('Schedule not found')
    if rows[0]['builtin']:
        raise ValueError('Built-in schedules cannot be deleted; switch them off instead')
    db.execute('DELETE FROM schedules WHERE id=?', (int(schedule_id),))
    return rows[0]


def run_schedule(row, manual=False):
    """Run one schedule now (in a worker thread so the clock never stalls) and plan its next run."""
    action = ACTIONS.get(row['action'])
    try:
        payload = json.loads(row['payload_json'] or '{}')
    except ValueError:
        payload = {}
    if manual:
        payload.setdefault('title', row['name'])
    nxt = reminders.to_utc(next_run(row['spec']))
    db.execute("UPDATE schedules SET last_run=?,next_run=?,last_status='running' WHERE id=?", (db.now(), nxt if not manual else row['next_run'] or nxt, row['id']))
    if row['action'] == 'prompt':
        payload.setdefault('title', row['name'])

    def work():
        try:
            if not action:
                raise ValueError('Unknown action ' + row['action'])
            result = action[0](payload)
            status = 'ok: ' + str(result)[:160]
        except Exception as exc:
            status = 'error: ' + str(exc)[:200]
            events.publish('schedule.failed', f"Schedule failed: {row['name']}", str(exc)[:200], 'warning', {'id': row['id']})
        db.execute('UPDATE schedules SET last_status=? WHERE id=?', (status, row['id']))
    threading.Thread(target=work, daemon=True).start()


def _run_due(now):
    stamp = now.astimezone(timezone.utc).isoformat(timespec='seconds')
    for row in db.rows("SELECT * FROM schedules WHERE enabled=1 AND next_run!='' AND next_run<=? ORDER BY next_run", (stamp,)):
        try:
            payload = json.loads(row['payload_json'] or '{}')
        except ValueError:
            payload = {}
        late = (now - reminders.from_iso(row['next_run'])).total_seconds() / 60
        if late > float(payload.get('catchup_minutes', 180)):
            db.execute("UPDATE schedules SET next_run=?,last_status=? WHERE id=?", (reminders.to_utc(next_run(row['spec'], now)), 'skipped (Sage was off)', row['id']))
            continue
        run_schedule(row)


# ------------------------------------------------------------------ the clock

def _loop():
    from . import planner
    last_schedule = last_plan = 0.0
    while not _stop.is_set():
        started = time.time()
        try:
            reminders.check_due()
        except Exception as exc:
            _log('reminders', exc)
        if started - last_schedule >= 10:
            last_schedule = started
            try:
                _run_due(reminders.local_now())
            except Exception as exc:
                _log('schedules', exc)
        if started - last_plan >= 30:
            last_plan = started
            try:
                planner.housekeeping()
            except Exception as exc:
                _log('planner', exc)
        _stop.wait(max(0.1, 1.0 - (time.time() - started)))


def _log(where, exc):
    try:
        db.log_action('scheduler.' + where, {}, 'error', str(exc))
    except Exception:
        pass


def start():
    global _thread
    seed_defaults()
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='sage-clock', daemon=True)
    _thread.start()
