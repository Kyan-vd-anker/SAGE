"""Chat commands that Sage understands without asking the model: reminders, timers, the work plan and "forget ...".
Small local models are unreliable at tool calling, so the common requests are handled deterministically here."""
import re

from . import db, entities, memory, planner, reminders, trust, userprofile

_SHOW_PROFILE = re.compile(r"^\W*(?:show|print|what(?:'s| is))\s+(?:me\s+)?(?:the\s+|my\s+)?(?:core\s+)?(?:profile|core profile)\s*[.?!]?$", re.I)
_SHOW_TIERS = re.compile(r"^\W*(?:show|print)\s+(?:me\s+)?(?:the\s+|my\s+)?memory\s+(?:tiers|stats|overview)\s*[.?!]?$", re.I)
_SHOW_ENTITIES = re.compile(r"^\W*(?:show|list)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:known\s+)?entities\s*[.?!]?$", re.I)
_STATE_OF_PLAY = re.compile(r"^\W*(?:show\s+(?:me\s+)?)?(?:the\s+)?state\s+of\s+play(?:\s+(?:for|of|on))?\s+(?P<name>.{2,80}?)\s*[.?!]?$", re.I)

_PLAN_QUERY = re.compile(
    r"\b(?:what(?:'s| is| are)?\s+(?:you|are you)\s+(?:working on|doing)|what(?:'s| is)\s+in\s+your\s+(?:queue|plan)|show\s+(?:me\s+)?(?:your|the)\s+(?:work\s+)?(?:plan|queue)|"
    r"what(?:'s| is)\s+(?:next|up next)\s+(?:in|on)\s+(?:your|the)\s+(?:plan|queue)|(?:your|the)\s+work\s+(?:plan|queue|order)|wat\s+ga\s+je\s+doen|wat\s+is\s+je\s+planning)\b", re.I)
_PRIORITIZE = [
    re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:do|work on|start|handle|take care of|tackle)\s+(?P<t>.+?)\s+(?:first|next|now)\s*[.!]?$", re.I),
    re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:prioriti[sz]e|move up|put)\s+(?P<t>.+?)(?:\s+(?:first|on top|at the top))?\s*[.!]?$", re.I),
    re.compile(r"^doe\s+(?P<t>.+?)\s+eerst\s*[.!]?$", re.I),
]
_NEEDS_QUERY = re.compile(r"\b(?:what\s+do\s+you\s+need\s+from\s+me|what\s+(?:are|is)\s+you\s+waiting\s+for|what(?:'s| is)\s+(?:blocking|stuck)|which\s+tasks\s+need\s+(?:my\s+)?(?:input|me|help)|wat\s+heb\s+je\s+van\s+me\s+nodig|waar\s+wacht\s+je\s+op)\b", re.I)
_AUTOPILOT_OFF = re.compile(r"\b(?:pause|stop|disable|turn off|switch off)\b.*\b(?:autopilot|background work|working on (?:my )?(?:tasks|projects))\b", re.I)
_AUTOPILOT_ON = re.compile(r"\b(?:resume|start|enable|turn on|switch on|continue)\b.*\b(?:autopilot|background work|working on (?:my )?(?:tasks|projects))\b", re.I)
_WINDOW = re.compile(r"\bonly\s+(?:work|run)\s+(?:between|from)\s+(?P<a>\d{1,2}(?::\d{2})?)\s*(?:and|to|-|until)\s*(?P<b>\d{1,2}(?::\d{2})?)\b", re.I)
_MAKE_PLAN = re.compile(r"^(?:please\s+|can you\s+|could you\s+)?(?:make|create|draw up|write|draft|build|prepare)(?:\s+me)?(?:\s+out)?(?:\s+(?:a|the))?\s+(?:project\s+)?plan\s+(?:for|to|of|on)\s+(?P<goal>.{6,})$", re.I)
_PLAN_OUT = re.compile(r"^(?:please\s+|can you\s+|could you\s+)?plan\s+out\s+(?:a\s+|the\s+)?(?:project\s+)?(?:for\s+|to\s+)?(?P<goal>.{6,})$", re.I)


def _hhmm(value):
    return value if ':' in value else f'{int(value):02d}:00'


def handle(text):
    """Return a reply when `text` is one of the commands above, otherwise None."""
    raw = text.strip()
    if not raw:
        return None
    reply = reminders.handle(raw)
    if reply is not None:
        return reply
    if trust.is_voice() and re.match(r"^\W*(?:please\s+)?(?:forget|vergeet)\b", raw, re.I):
        return "I don't forget memories by voice. Use the Planner page or the keyboard for that."
    forgotten = memory.forget(raw)
    if forgotten is not None:
        if not forgotten:
            return "I couldn't find a saved memory like that."
        return 'Okay, I have forgotten: ' + '; '.join(forgotten) + '. (You can restore it from the Planner page.)'
    if _SHOW_PROFILE.match(raw):
        text_block, _ = userprofile.core()
        return text_block or 'My core profile is empty. Pin a memory or tell me "remember that ..." and it will appear here.'
    if _SHOW_TIERS.match(raw):
        counts = memory.tier_counts()
        return ('Memory tiers: ' + ', '.join(f'{name} {counts[name]}' for name in memory.TIERS) +
                f". {counts['suggested']} suggestions are waiting for review and {counts['superseded']} older notes were replaced by newer ones.")
    if _SHOW_ENTITIES.match(raw):
        rows = entities.listing(15)
        return 'Known entities: ' + '; '.join(f"{r['name']} ({r['kind']}, {r['memories']} notes)" for r in rows) if rows else 'I do not know any entities yet.'
    match = _STATE_OF_PLAY.match(raw)
    if match:
        name = match.group('name').strip().lower()
        for project in db.rows('SELECT id,name FROM projects ORDER BY id'):
            if name in project['name'].lower() or project['name'].lower() in name:
                return userprofile.state_text(project['id']) or f"I have not written a state of play for {project['name']} yet. I will after we talk about it."
        return f'I do not have a project called "{match.group("name")}".'
    window = _WINDOW.search(raw)
    if window:
        planner.set_autopilot(window='%s-%s' % (_hhmm(window.group('a')), _hhmm(window.group('b'))))
        return f"Okay. I will only work on tasks between {_hhmm(window.group('a'))} and {_hhmm(window.group('b'))}."
    if _AUTOPILOT_OFF.search(raw):
        planner.set_autopilot(False)
        return 'Autopilot is paused. I will not start new tasks on my own until you say "resume autopilot".'
    if _AUTOPILOT_ON.search(raw) and len(raw) < 80:
        planner.set_autopilot(True)
        return 'Autopilot is on. I will work through the queue whenever I am idle.'
    if _NEEDS_QUERY.search(raw):
        return planner.describe_needs()
    if _PLAN_QUERY.search(raw):
        return planner.describe()
    for pattern in _PRIORITIZE:
        match = pattern.match(raw.rstrip('?'))
        if match:
            task = planner.find_task(match.group('t'))
            if task:
                try:
                    planner.prioritize(task['id'])
                except ValueError as exc:
                    return str(exc)
                return f"Done. \"{task['title']}\" is now first in my queue."
            break
    match = _MAKE_PLAN.match(raw) or _PLAN_OUT.match(raw)
    if match:
        goal = match.group('goal').strip(' .!?')
        if re.match(r"^(?:my|the)\s+(?:day|week|morning|evening)\b|^(?:today|tomorrow)\b", goal, re.I):
            return None
        planner.queue_plan(goal)
        return (f'I am drafting a plan for "{goal[:100]}" in the background. It usually takes a minute or two on the local model. '
                'I will notify you when it is ready, and you can reorder the tasks on the Planner page.')
    return None
