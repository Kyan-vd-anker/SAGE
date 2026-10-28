"""Durable, privacy-conscious checkpoints for interrupted PC-control tasks."""
import json
from . import db


PC_TOOL_NAMES = {
    'inspect_linux_screen', 'linux_desktop_action', 'run_linux_command',
    'install_system_packages', 'open_browser_url', 'linux_file_action',
    'launch_app', 'workspace_files', 'workspace_write',
}


def _label(name, arguments):
    args = arguments if isinstance(arguments, dict) else {}
    if name == 'inspect_linux_screen':
        return 'Checked the current screen'
    if name == 'linux_desktop_action':
        return {
            'click': 'Clicked a screen control',
            'type_text': 'Entered text on the screen',
            'press_key': 'Pressed a keyboard key',
            'scroll': 'Scrolled the screen',
        }.get(args.get('action'), 'Used a desktop control')
    if name == 'run_linux_command':
        return 'Ran a Linux command'
    if name == 'install_system_packages':
        return 'Installed approved software'
    if name == 'open_browser_url':
        return 'Opened a website'
    if name == 'launch_app':
        return 'Opened an application'
    if name == 'linux_file_action':
        action = str(args.get('action') or 'handled')
        return {'copy': 'Copied a file', 'move': 'Moved a file', 'write': 'Wrote a file', 'delete': 'Deleted a file', 'open': 'Opened a file'}.get(action, 'Handled a file')
    if name == 'workspace_files':
        return 'Read Sage workspace files'
    if name == 'workspace_write':
        return 'Wrote a Sage workspace file'
    return 'Completed a computer action'


def create(goal):
    goal = str(goal).strip()[:4000] or 'Computer task'
    now = db.now()
    return db.execute(
        'INSERT INTO computer_tasks(goal,status,checkpoint,steps_json,pending_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
        (goal, 'running', 'Preparing the computer task', '[]', '', now, now),
    )


def get(task_id):
    rows = db.rows('SELECT * FROM computer_tasks WHERE id=?', (int(task_id),))
    if not rows:
        return None
    item = rows[0]
    try:
        item['steps'] = json.loads(item.pop('steps_json') or '[]')
    except (ValueError, TypeError):
        item['steps'] = []
    try:
        item['pending_step'] = json.loads(item.pop('pending_json') or 'null')
    except (ValueError, TypeError):
        item['pending_step'] = None
    return item


def get_recoverable():
    ids = db.rows("SELECT id FROM computer_tasks WHERE status='interrupted' ORDER BY updated_at DESC LIMIT 10")
    return [item for row in ids if (item := get(row['id']))]


def begin_step(task_id, name, arguments):
    label = _label(name, arguments)
    pending = json.dumps({'label': label}, ensure_ascii=False)
    db.execute('UPDATE computer_tasks SET status=?,checkpoint=?,pending_json=?,updated_at=? WHERE id=?',
               ('running', f'In progress: {label}', pending, db.now(), int(task_id)))
    return label


def finish_step(task_id, label, succeeded):
    task = get(task_id)
    if not task:
        return
    steps = task.get('steps', [])
    steps.append({'label': str(label)[:160], 'status': 'completed' if succeeded else 'failed', 'at': db.now()})
    steps = steps[-30:]
    checkpoint = f'{label} ' + ('finished' if succeeded else 'did not finish')
    db.execute('UPDATE computer_tasks SET status=?,checkpoint=?,steps_json=?,pending_json=?,updated_at=? WHERE id=?',
               ('running', checkpoint, json.dumps(steps, ensure_ascii=False), '', db.now(), int(task_id)))


def set_status(task_id, status, checkpoint=None):
    if status not in {'running', 'interrupted', 'waiting_approval', 'completed', 'dismissed'}:
        raise ValueError('Invalid computer task status')
    if checkpoint is None:
        db.execute('UPDATE computer_tasks SET status=?,updated_at=? WHERE id=?', (status, db.now(), int(task_id)))
    else:
        db.execute('UPDATE computer_tasks SET status=?,checkpoint=?,updated_at=? WHERE id=?',
                   (status, str(checkpoint)[:500], db.now(), int(task_id)))


def recover_on_startup():
    running = db.rows("SELECT id,pending_json FROM computer_tasks WHERE status='running'")
    for row in running:
        try:
            pending = json.loads(row.get('pending_json') or 'null')
        except (ValueError, TypeError):
            pending = None
        if pending:
            checkpoint = f"Sage restarted during: {pending.get('label','a computer action')}. It may have partly completed; inspect the current state before deciding what to do."
        else:
            checkpoint = 'Sage restarted during this task. Review completed steps and inspect the current state before continuing.'
        set_status(row['id'], 'interrupted', checkpoint)


def resume(task_id):
    task = db.rows("SELECT id,status FROM computer_tasks WHERE id=?", (int(task_id),))
    if not task or task[0]['status'] != 'interrupted':
        raise ValueError('This computer task is not waiting to be resumed.')
    db.execute('UPDATE computer_tasks SET status=?,checkpoint=?,resume_count=resume_count+1,updated_at=? WHERE id=?',
               ('running', 'Rechecking the current PC state before continuing', db.now(), int(task_id)))


def dismiss(task_id):
    task = db.rows("SELECT id,status FROM computer_tasks WHERE id=?", (int(task_id),))
    if not task or task[0]['status'] != 'interrupted':
        raise ValueError('This computer task is not waiting for a decision.')
    set_status(task_id, 'dismissed', 'Dismissed by the user')


def recovery_context(task_id):
    task = get(task_id)
    if not task:
        raise ValueError('The saved computer task could not be found.')
    done = [f"- {item['label']} ({item['status']})" for item in task['steps']]
    pending = task.get('pending_step')
    text = [
        'The user chose to resume this saved computer task.',
        'Original goal: ' + task['goal'],
        'Previously completed steps:\n' + ('\n'.join(done) if done else '- None recorded'),
        'Last checkpoint: ' + task['checkpoint'],
    ]
    if pending:
        text.append('Interrupted step: ' + str(pending.get('label', 'Unknown action')) + '. This action may have partly completed. Inspect the live state and verify whether it already happened; never blindly repeat it.')
    text.append('First inspect the current desktop. Then tell the user briefly what remains before taking the next action. Continue toward the original goal, respecting all normal approval requirements. Do not claim completed steps beyond the saved record and current inspection.')
    return '\n\n'.join(text)
