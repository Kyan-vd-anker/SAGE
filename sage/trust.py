"""Who is asking? Requests that arrive by voice get less power than the keyboard.

Anyone within earshot (a TV, a visitor, a voice through the window) can trigger the speaker, so voice may
read things and do small, reversible jobs, but nothing else runs without an approval on the dashboard (or the
physical button on the speaker for the tools in VOICE_BUTTON_OK).

    with trust.source('voice'):
        agent.respond(text)

The check itself lives in tools.permission(): while the source is 'voice', every tool that is not in
VOICE_ALLOWED is treated as 'confirm', so it is queued as an approval instead of running.
"""
import threading
from contextlib import contextmanager

_local = threading.local()

# Safe by voice: reading, searching, calculating, timers and reminders, and adding a workshop note.
VOICE_ALLOWED = frozenset({
    'get_outlook_calendar', 'list_projects', 'search_memory', 'search_workshop_notebook', 'calculate_engineering',
    'list_inventory', 'list_failures_and_lessons', 'list_troubleshooting_guides', 'list_engineering_files',
    'check_project_parts', 'get_project_brief', 'search_related_knowledge', 'check_task_dependencies',
    'review_project_readiness', 'inspect_engineering_asset', 'audit_bom_file', 'analyze_measurement_run',
    'compare_measurement_runs', 'get_project_timeline', 'analyze_task_graph', 'compare_bom_revisions',
    'explain_memory_provenance', 'calculator', 'web_search',
    'add_workshop_entry',
    'set_reminder', 'start_timer', 'list_reminders', 'cancel_reminder', 'snooze_reminder', 'list_schedules',
    'show_plan', 'move_task', 'pause_task', 'set_autopilot',
    # Computer use, shell commands, and software installation always require keyboard/dashboard approval.
})


@contextmanager
def source(name):
    previous = getattr(_local, 'name', '')
    _local.name = name
    try:
        yield
    finally:
        _local.name = previous


def current():
    return getattr(_local, 'name', '')


def is_voice():
    return current() == 'voice'


def voice_blocks(tool_name):
    """True when this tool may not run right now because the request came by voice. Remembers it for the audit trail."""
    if is_voice() and tool_name not in VOICE_ALLOWED:
        blocked = getattr(_local, 'blocked', None)
        if blocked is None:
            blocked = _local.blocked = []
        if tool_name not in blocked:
            blocked.append(tool_name)
        return True
    return False


def drain_blocked():
    """Tools that voice was refused during this thread's last request (and clear the list)."""
    blocked = getattr(_local, 'blocked', None) or []
    _local.blocked = []
    return blocked
