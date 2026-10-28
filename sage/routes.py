"""HTTP routes for events, reminders, schedules, the work plan and memory management.
server.py calls get()/post() first; they return (handled, value). Errors are raised as ValueError (-> HTTP 400)."""
import re
from urllib.parse import parse_qs

from . import db, events, memory, planner, reminders, scheduler

NOT_HANDLED = (False, None)


def _int(value, name='id'):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number')


def get(path, query):
    """query is the parsed query string (dict of lists)."""
    q = lambda key, default='': (query.get(key) or [default])[0]
    if path == '/api/events':
        if q('since') == '':
            items = events.recent(0, 30)
            return True, {'events': items, 'cursor': events.cursor()}
        since = _int(q('since'), 'since')
        wait = max(0.0, min(float(q('wait', '0') or 0), 30.0))
        items = events.wait_for(since, wait) if wait else events.recent(since, 100)
        return True, {'events': items, 'cursor': items[-1]['id'] if items else max(since, events.cursor())}
    if path == '/api/reminders':
        snap = reminders.snapshot()
        snap['recent'] = db.rows("SELECT id,kind,title,due_at,status FROM reminders WHERE status IN ('done','cancelled') ORDER BY id DESC LIMIT 8")
        return True, snap
    if path == '/api/schedules':
        return True, {'schedules': scheduler.list_schedules(), 'actions': {k: v[1] for k, v in scheduler.ACTIONS.items()}}
    if path == '/api/plan':
        return True, planner.plan_view()
    m = re.fullmatch(r'/api/plan/task/(\d+)', path)
    if m:
        return True, planner.task_detail(int(m.group(1)))
    if path == '/api/memories':
        return True, memory.list_memories(q('status', 'active') or 'active', q('q').strip())
    if path == '/api/voice/state':
        from . import voice
        wait = max(0.0, min(float(q('wait', '0') or 0), 30.0))
        return True, voice.wait_state(_int(q('since', '-1'), 'since'), wait) if wait else voice.state_snapshot()
    if path == '/api/voice/audit':
        from . import voice
        return True, {'summary': voice.audit_summary(), 'audit': voice.audit_rows(_int(q('limit', '50'), 'limit'))}
    return NOT_HANDLED


def post(path, body):
    if path == '/api/shortcuts/awake':
        return True, scheduler.action_awake_brief()
    if path == '/api/plan/approve-blocked':
        if body.get('confirmed') is not True:
            raise ValueError('Explicit confirmation is required to approve every blocked task as complete')
        return True, {'ok': True, **planner.approve_blocked_tasks()}
    if path == '/api/reminders':
        kind = body.get('kind', 'reminder')
        if kind not in ('reminder', 'timer', 'alarm'):
            raise ValueError('Kind must be reminder, timer or alarm')
        now = reminders.local_now()
        if kind == 'timer':
            delta = reminders.parse_duration(str(body.get('duration') or body.get('when') or ''))
            if not delta:
                raise ValueError('Enter a duration such as 10 minutes')
            rid = reminders.create('timer', body.get('title') or f'{reminders.format_duration(delta)} timer', now + delta, '', 'web')
            return True, {'id': rid}
        repeat, rest = reminders.extract_repeat(str(body.get('when', '')) + ' ' + str(body.get('repeat', '')))
        found = reminders.extract_time(rest, now, 'am' if repeat else 'next')
        if not found and repeat.startswith('every:'):
            found = (now + reminders.timedelta(minutes=int(repeat.split(':')[1])), '')
        if not found:
            raise ValueError('I could not understand the time. Try "tomorrow 18:00" or "in 20 minutes".')
        if found[0] <= now:
            raise ValueError('That time has already passed')
        title = str(body.get('title', '')).strip()
        if not title:
            raise ValueError('Give the reminder a title')
        return True, {'id': reminders.create(kind, title, found[0], repeat, 'web')}
    m = re.fullmatch(r'/api/reminders/(\d+)/(done|cancel|snooze)', path)
    if m:
        rid, action = int(m.group(1)), m.group(2)
        if action == 'done':
            reminders.done(rid)
        elif action == 'cancel':
            reminders.cancel(rid)
        else:
            reminders.snooze(rid, body.get('minutes', 10))
        return True, {'ok': True}
    if path == '/api/schedules':
        payload = body.get('payload') if isinstance(body.get('payload'), dict) else {}
        if body.get('prompt'):
            payload = {**payload, 'prompt': body['prompt'], 'title': body.get('name', '')}
        return True, scheduler.create_schedule(body.get('name', ''), body.get('when', ''), body.get('action', 'prompt'), payload)
    m = re.fullmatch(r'/api/schedules/(\d+)(?:/(run|delete))?', path)
    if m:
        sid, action = int(m.group(1)), m.group(2)
        if action == 'delete':
            scheduler.delete_schedule(sid)
            return True, {'ok': True}
        if action == 'run':
            rows = db.rows('SELECT * FROM schedules WHERE id=?', (sid,))
            if not rows:
                raise ValueError('Schedule not found')
            scheduler.run_schedule(rows[0], manual=True)
            return True, {'ok': True}
        return True, scheduler.update_schedule(sid, name=body.get('name'), when=body.get('when'), enabled=body.get('enabled'), payload=body.get('payload'))
    if path == '/api/plan/reorder':
        ids = body.get('task_ids')
        if not isinstance(ids, list):
            raise ValueError('task_ids must be a list')
        return True, planner.reorder(ids)
    if path == '/api/plan/move':
        return True, planner.move(_int(body.get('task_id'), 'task_id'), _int(body.get('position'), 'position'))
    if path == '/api/plan/run-now':
        return True, {'job_id': planner.run_now(_int(body.get('task_id'), 'task_id'))}
    if path == '/api/plan/autopilot':
        return True, planner.set_autopilot(body.get('enabled'), body.get('window'))
    if path == '/api/plan/generate':
        pid = body.get('project_id')
        return True, {'job_id': planner.queue_plan(body.get('goal', ''), _int(pid, 'project_id') if pid else None)}
    m = re.fullmatch(r'/api/plan/task/(\d+)', path)
    if m:
        fields = {k: body.get(k) for k in ('assignee', 'paused', 'estimate_minutes', 'kind', 'priority', 'title', 'description', 'acceptance', 'status')}
        planner.update_task(int(m.group(1)), **fields)
        return True, planner.plan_view()
    if path == '/api/plan/task':
        return True, planner.create_task(body.get('title'), body.get('description', ''), body.get('assignee', 'user'), body.get('project_id') or None, body.get('kind'), body.get('acceptance', ''))
    m = re.fullmatch(r'/api/plan/task/(\d+)/input', path)
    if m:
        return True, planner.give_input(int(m.group(1)), body.get('text', ''), bool(body.get('mark_done')), bool(body.get('retry')))
    m = re.fullmatch(r'/api/plan/task/(\d+)/delete', path)
    if m:
        return True, planner.delete_task(int(m.group(1)))
    m = re.fullmatch(r'/api/memories/(\d+)', path)
    if m:
        return True, memory.update_memory(int(m.group(1)), pinned=body.get('pinned'), importance=body.get('importance'), status=body.get('status'), content=body.get('content'))
    return NOT_HANDLED
