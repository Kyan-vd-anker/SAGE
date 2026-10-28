"""Tools for reminders, timers, schedules, the work plan and memory management.
Every tool here is reversible (nothing is deleted for good), so none needs approval."""
import re

from . import db, memory, planner, reminders, scheduler


def _tool(name, description, properties=None, required=None):
    schema = {'type': 'object', 'properties': properties or {}}
    if required:
        schema['required'] = required
    return (name, description, schema)


BUILTINS = [
    _tool('set_reminder', 'Set a reminder or alarm. `when` is plain language: "tomorrow at 18:00", "in 20 minutes", "friday 9am", "every day at 8". Use this whenever the user asks to be reminded of something.',
          {'title': {'type': 'string'}, 'when': {'type': 'string'}, 'repeat': {'type': 'string', 'description': 'optional, e.g. "every monday"'}}, ['title', 'when']),
    _tool('start_timer', 'Start a countdown timer, e.g. duration "10 minutes" or "1 hour 30 minutes". The timer rings on the wall display and in the web page.',
          {'duration': {'type': 'string'}, 'label': {'type': 'string'}}, ['duration']),
    _tool('list_reminders', 'List the active reminders, alarms and timers, and anything that is ringing now.'),
    _tool('cancel_reminder', 'Cancel a reminder, alarm or timer by id, or by words from its title.',
          {'id': {'type': 'integer'}, 'query': {'type': 'string'}}),
    _tool('snooze_reminder', 'Snooze a ringing reminder (default 10 minutes). Without an id, snoozes everything that is ringing.',
          {'id': {'type': 'integer'}, 'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440}}),
    _tool('schedule_task', 'Ask Sage to do something on a schedule and post the answer in chat, e.g. every monday at 9 "summarise the ROV project". `when` is plain language.',
          {'name': {'type': 'string'}, 'when': {'type': 'string'}, 'prompt': {'type': 'string'}}, ['name', 'when', 'prompt']),
    _tool('list_schedules', 'List Sage\'s recurring schedules (morning brief, backups, your own).'),
    _tool('manage_schedule', 'Turn a schedule on or off, run it now, or delete one that the user created.',
          {'id': {'type': 'integer'}, 'action': {'type': 'string', 'enum': ['enable', 'disable', 'run', 'delete']}}, ['id', 'action']),
    _tool('plan_project', 'Draft a structured plan for a goal (milestones, ordered tasks, estimates, acceptance criteria, who does each task). Runs in the background and notifies when ready. Optionally extend an existing project.',
          {'goal': {'type': 'string'}, 'project_id': {'type': 'integer'}}, ['goal']),
    _tool('show_plan', 'Show Sage\'s work queue: what it is doing now, what is next in order with time estimates, what is waiting for the user, and what is blocked.'),
    _tool('move_task', 'Change the order Sage works in. `task` is a task id or words from its title; `position` is a number (1 = first) or "first" / "last".',
          {'task': {'type': 'string'}, 'position': {'type': 'string'}}, ['task', 'position']),
    _tool('set_task_assignee', 'Say who does a task: "sage" (Sage works on it in the background) or "user" (the user does it).',
          {'task': {'type': 'string'}, 'assignee': {'type': 'string', 'enum': ['sage', 'user']}}, ['task', 'assignee']),
    _tool('pause_task', 'Pause or resume a task so Sage skips it or picks it up again.',
          {'task': {'type': 'string'}, 'paused': {'type': 'boolean'}}, ['task', 'paused']),
    _tool('run_task_now', 'Put a task first in the queue and have Sage start it as soon as the user stops chatting.',
          {'task': {'type': 'string'}}, ['task']),
    _tool('set_autopilot', 'Turn Sage\'s background work on or off, and optionally limit it to a time window like "22:00-07:00".',
          {'enabled': {'type': 'boolean'}, 'window': {'type': 'string'}}),
    _tool('forget_memory', 'Forget (archive) a saved memory that matches the given words. It can be restored from the Planner page.',
          {'query': {'type': 'string'}}, ['query']),
    _tool('pin_memory', 'Pin a memory so Sage always keeps it in mind, or unpin it.',
          {'query': {'type': 'string'}, 'pinned': {'type': 'boolean'}}, ['query']),
]
NAMES = {b[0] for b in BUILTINS}

TRIGGERS = {
    'set_reminder': ('remind me', 'reminder', 'alarm', 'wake me', 'herinner', 'wekker'),
    'start_timer': ('timer', 'countdown', 'aftellen'),
    'list_reminders': ('my reminders', 'my timers', 'what timers', 'what reminders', 'herinneringen'),
    'cancel_reminder': ('cancel the timer', 'cancel the reminder', 'delete the reminder', 'annuleer'),
    'snooze_reminder': ('snooze',),
    'schedule_task': ('every day', 'every week', 'every morning', 'every monday', 'every weekday', 'on a schedule', 'recurring', 'elke dag'),
    'list_schedules': ('schedules', 'scheduled', 'recurring tasks'),
    'manage_schedule': ('turn off the schedule', 'disable the schedule', 'run the schedule', 'delete the schedule', 'morning brief'),
    'plan_project': ('plan a project', 'make a plan', 'create a plan', 'plan for', 'draft a plan', 'project plan', 'plan out'),
    'show_plan': ('your plan', 'your queue', 'work queue', 'what are you working on', 'what is next', 'what\'s next', 'work order', 'the plan'),
    'move_task': ('first', 'prioritize', 'prioritise', 'reorder', 'move task', 'work order', 'before'),
    'set_task_assignee': ('assign', 'i will do', "i'll do", 'you do', 'let me do', 'give this to'),
    'pause_task': ('pause task', 'skip task', 'resume task', 'hold off on'),
    'run_task_now': ('start the task now', 'run the task now', 'do it now', 'work on it now'),
    'set_autopilot': ('autopilot', 'background work', 'work at night', 'only work between'),
    'forget_memory': ('forget',),
    'pin_memory': ('pin this memory', 'always remember', 'pin memory'),
}


def _task(ref):
    ref = str(ref).strip()
    found = planner.find_task(ref)
    if not found:
        raise ValueError(f'I could not find an open task matching "{ref}". Say "show the plan" to see the tasks.')
    return found


def _position(value, last):
    text = str(value).strip().lower()
    if text in ('first', 'top', 'next', '1'):
        return 1
    if text in ('last', 'bottom', 'end'):
        return max(1, last)
    if text.isdigit():
        return int(text)
    raise ValueError('Position must be a number, "first" or "last"')


def run(name, args):
    if name == 'set_reminder':
        now = reminders.local_now()
        repeat = args.get('repeat', '') or ''
        text = args['when'] + (' ' + repeat if repeat else '')
        spec, rest = reminders.extract_repeat(text)
        found = reminders.extract_time(rest, now, 'am' if spec else 'next')
        if not found and spec.startswith('every:'):
            found = (now + reminders.timedelta(minutes=int(spec.split(':')[1])), '')
        if not found:
            raise ValueError('I could not understand the time. Try "tomorrow at 18:00" or "in 20 minutes".')
        due = found[0]
        if due <= now:
            raise ValueError('That time has already passed.')
        rid = reminders.create('reminder', args['title'], due, spec, 'tool')
        return {'id': rid, 'title': args['title'], 'when': reminders.humanize(due, now, False), 'repeat': reminders.describe_repeat(spec)}
    if name == 'start_timer':
        delta = reminders.parse_duration(args['duration'])
        if not delta:
            raise ValueError('I could not understand that duration. Try "10 minutes".')
        due = reminders.local_now() + delta
        rid = reminders.create('timer', args.get('label') or f'{reminders.format_duration(delta)} timer', due, '', 'tool')
        return {'id': rid, 'rings_in': reminders.format_duration(delta), 'at': due.strftime('%H:%M:%S')}
    if name == 'list_reminders':
        snap = reminders.snapshot()
        return {'ringing': snap['alerts'], 'active': snap['active']}
    if name == 'cancel_reminder':
        if args.get('id'):
            row = reminders.cancel(args['id'])
            return {'cancelled': row['title']}
        text = reminders._cancel_from_text('cancel ' + str(args.get('query', 'timer')))
        return {'result': text}
    if name == 'snooze_reminder':
        minutes = int(args.get('minutes') or 10)
        ids = [args['id']] if args.get('id') else [r['id'] for r in reminders.waiting()]
        for rid in ids:
            reminders.snooze(rid, minutes)
        return {'snoozed': len(ids), 'minutes': minutes}
    if name == 'schedule_task':
        return scheduler.create_schedule(args['name'], args['when'], 'prompt', {'prompt': args['prompt'], 'title': args['name']})
    if name == 'list_schedules':
        return scheduler.list_schedules()
    if name == 'manage_schedule':
        sid, action = int(args['id']), args['action']
        if action == 'delete':
            return {'deleted': scheduler.delete_schedule(sid)['name']}
        if action == 'run':
            scheduler.run_schedule(db.rows('SELECT * FROM schedules WHERE id=?', (sid,))[0], manual=True)
            return {'started': True}
        return scheduler.update_schedule(sid, enabled=(action == 'enable'))
    if name == 'plan_project':
        jid = planner.queue_plan(args['goal'], args.get('project_id'))
        return {'job_id': jid, 'status': 'queued', 'note': 'Sage is drafting the plan in the background and will notify you when it is ready.'}
    if name == 'show_plan':
        view = planner.plan_view()
        return {'text': planner.describe(), 'running': view['running'], 'queue': view['queue'][:8], 'waiting_for_user': view['mine'][:6], 'blocked': view['blocked'][:6]}
    if name == 'move_task':
        task = _task(args['task'])
        view = planner.move(task['id'], _position(args['position'], len(planner._visible_order())))
        return {'moved': task['title'], 'queue': [i['title'] for i in view['queue'][:6]]}
    if name == 'set_task_assignee':
        task = _task(args['task'])
        planner.update_task(task['id'], assignee=args['assignee'])
        return {'task': task['title'], 'assignee': args['assignee']}
    if name == 'pause_task':
        task = _task(args['task'])
        planner.update_task(task['id'], paused=bool(args['paused']))
        return {'task': task['title'], 'paused': bool(args['paused'])}
    if name == 'run_task_now':
        task = _task(args['task'])
        jid = planner.run_now(task['id'])
        return {'task': task['title'], 'job_id': jid, 'note': 'Sage starts as soon as you stop chatting.'}
    if name == 'set_autopilot':
        return planner.set_autopilot(args.get('enabled'), args.get('window'))
    if name in ('forget_memory', 'pin_memory'):
        wanted = memory.keywords(args['query'])
        if not wanted:
            raise ValueError('Tell me which memory, in a few words')
        best = None
        for row in db.rows("SELECT id,content FROM memories WHERE status='active' ORDER BY updated_at DESC LIMIT 500"):
            overlap = len(wanted & memory.keywords(row['content']))
            if overlap and (not best or overlap > best[0]):
                best = (overlap, row)
        if not best:
            raise ValueError('I could not find a memory like that')
        if name == 'forget_memory':
            memory.update_memory(best[1]['id'], status='archived')
            return {'forgotten': best[1]['content']}
        memory.update_memory(best[1]['id'], pinned=bool(args.get('pinned', True)), importance=5 if args.get('pinned', True) else None)
        return {'pinned': bool(args.get('pinned', True)), 'memory': best[1]['content']}
    raise ValueError('Unknown tool')
