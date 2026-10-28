"""Reminders, timers and alarms: natural-language times (English + Dutch), storage and firing.

    reminders.handle("remind me to call mum tomorrow at 18:00")      -> "Okay, I'll remind you ..."
    reminders.handle("set a timer for 10 minutes for the pasta")     -> "Timer set: ..."
    reminders.check_due()                                              # called every second by the scheduler
"""
import re
from datetime import datetime, timedelta, timezone

from . import db, events

# ------------------------------------------------------------------ vocabulary

WEEKDAYS = {
    'monday': 0, 'mon': 0, 'maandag': 0, 'tuesday': 1, 'tue': 1, 'tues': 1, 'dinsdag': 1,
    'wednesday': 2, 'wed': 2, 'woensdag': 2, 'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3, 'donderdag': 3,
    'friday': 4, 'fri': 4, 'vrijdag': 4, 'saturday': 5, 'sat': 5, 'zaterdag': 5, 'sunday': 6, 'sun': 6, 'zondag': 6,
}
WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MONTHS = {
    'january': 1, 'jan': 1, 'januari': 1, 'february': 2, 'feb': 2, 'februari': 2, 'march': 3, 'mar': 3, 'maart': 3,
    'april': 4, 'apr': 4, 'may': 5, 'mei': 5, 'june': 6, 'jun': 6, 'juni': 6, 'july': 7, 'jul': 7, 'juli': 7,
    'august': 8, 'aug': 8, 'augustus': 8, 'september': 9, 'sep': 9, 'sept': 9, 'october': 10, 'oct': 10, 'oktober': 10,
    'okt': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
}
NUMBER_WORDS = {
    'a': 1, 'an': 1, 'one': 1, 'een': 1, 'één': 1, 'two': 2, 'twee': 2, 'three': 3, 'drie': 3, 'four': 4, 'vier': 4,
    'five': 5, 'vijf': 5, 'six': 6, 'zes': 6, 'seven': 7, 'zeven': 7, 'eight': 8, 'acht': 8, 'nine': 9, 'negen': 9,
    'ten': 10, 'tien': 10, 'eleven': 11, 'elf': 11, 'twelve': 12, 'twaalf': 12, 'fifteen': 15, 'vijftien': 15,
    'twenty': 20, 'twintig': 20, 'thirty': 30, 'dertig': 30, 'forty': 40, 'veertig': 40, 'sixty': 60, 'zestig': 60,
}
UNITS = {
    's': 1, 'sec': 1, 'secs': 1, 'second': 1, 'seconds': 1, 'seconde': 1, 'seconden': 1,
    'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60, 'minuut': 60, 'minuten': 60,
    'h': 3600, 'hr': 3600, 'hrs': 3600, 'hour': 3600, 'hours': 3600, 'uur': 3600, 'uren': 3600,
    'd': 86400, 'day': 86400, 'days': 86400, 'dag': 86400, 'dagen': 86400,
    'w': 604800, 'week': 604800, 'weeks': 604800, 'weken': 604800,
}
DAY_PARTS = {'morning': 9, 'ochtend': 9, 'ochtends': 9, 'afternoon': 15, 'middag': 14, 'middags': 14, 'evening': 19, 'avond': 19,
             'avonds': 19, 'night': 22, 'nacht': 22, 'noon': 12, 'midnight': 0}

_WD = '|'.join(sorted(WEEKDAYS, key=len, reverse=True))
_MON = '|'.join(sorted(MONTHS, key=len, reverse=True))
_NUM = r'\d+(?:[.,]\d+)?|' + '|'.join(sorted(NUMBER_WORDS, key=len, reverse=True))
_UNIT = '|'.join(sorted(UNITS, key=len, reverse=True))

DURATION_PART = rf'(?:(?:{_NUM})\s*(?:{_UNIT})\b)'
SPECIAL_DURATION = (r'(?:half\s+an\s+hour|half\s+hour|a\s+quarter\s+of\s+an\s+hour|quarter\s+of\s+an\s+hour|a\s+quarter|een\s+half\s+uur|'
                    r'half\s+uur|anderhalf\s+uur|een\s+kwartier|kwartier|an\s+hour\s+and\s+a\s+half|one\s+and\s+a\s+half\s+hours?)')
DURATION_RE = re.compile(rf'(?<![\w:])(?P<d>{SPECIAL_DURATION}|{DURATION_PART}(?:\s*(?:,|and|en|\+)?\s*{DURATION_PART})*)(?![\w:])', re.I)
REL_RE = re.compile(rf'\b(?P<prefix>in|over|binnen|after|na|within)\s+(?P<d>{SPECIAL_DURATION}|{DURATION_PART}(?:\s*(?:,|and|en|\+)?\s*{DURATION_PART})*)(?![\w:])', re.I)

DAY_RE = re.compile(
    rf"\b(?P<day>day\s+after\s+tomorrow|overmorgen|tomorrow|tmrw|morgen|today|vandaag|tonight|vanavond|vannacht|"
    rf"(?:(?:next|volgende|on|op|this|deze|coming|aanstaande|komende)\s+)?(?:{_WD}))\b", re.I)
PART_RE = re.compile(r"(?:'s\s+)?\b(?P<part>morning|afternoon|evening|night|noon|midnight|ochtend|ochtends|middag|middags|avond|avonds|nacht)\b", re.I)
TIME_RE = re.compile(
    r"(?:(?P<pre>\bat|\bom|@|\baround|\brond|\bby|\bvoor)\s*)?"
    r"(?<![\d:.\-/])(?P<h>\d{1,2})(?:[:.h](?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?|uur|u\b|h\b)?(?![\d:])", re.I)
ISO_RE = re.compile(r'\b(?P<y>20\d\d)-(?P<mo>\d{1,2})-(?P<d>\d{1,2})(?:[ t](?P<h>\d{1,2}):(?P<mi>\d{2}))?\b', re.I)
DMY_RE = re.compile(r'\b(?P<d>\d{1,2})[/-](?P<mo>\d{1,2})(?:[/-](?P<y>\d{2,4}))?\b')
DATE_NAMED_RE = re.compile(rf'\b(?:(?:on|op)\s+)?(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{_MON})|(?P<m2>{_MON})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)\b', re.I)
NEXT_WEEK_RE = re.compile(r'\b(?:next\s+week|volgende\s+week)\b', re.I)

# ------------------------------------------------------------------ small helpers

def local_now():
    return datetime.now().astimezone()


def to_utc(dt_value):
    return dt_value.astimezone(timezone.utc).isoformat(timespec='seconds')


def from_iso(text):
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.astimezone()


def _number(token):
    token = token.lower().replace(',', '.')
    if token in NUMBER_WORDS:
        return float(NUMBER_WORDS[token])
    return float(token)


def parse_duration(text):
    """'1 hour 30 minutes', '90 sec', 'half an hour', '1h30m', 'een kwartier' -> timedelta (or None)."""
    lower = text.lower()
    special = {'half an hour': 1800, 'half hour': 1800, 'a quarter of an hour': 900, 'quarter of an hour': 900, 'a quarter': 900,
               'een half uur': 1800, 'half uur': 1800, 'anderhalf uur': 5400, 'een kwartier': 900, 'kwartier': 900,
               'an hour and a half': 5400, 'one and a half hour': 5400, 'one and a half hours': 5400}
    for phrase, seconds in special.items():
        if re.search(rf'(?<!\w){re.escape(phrase)}(?!\w)', lower):
            return timedelta(seconds=seconds)
    total = 0.0
    found = False
    for match in re.finditer(rf'(?P<n>{_NUM})\s*(?P<u>{_UNIT})\b', lower):
        total += _number(match.group('n')) * UNITS[match.group('u')]
        found = True
    return timedelta(seconds=total) if found and total > 0 else None


def _hour_candidates(hour, minute, ap, near_day, now_dt, has_day, part=None, prefer='next'):
    """Return the hour (0-23) to use for an ambiguous 12-hour clock reading."""
    if ap:
        ap = ap.lower().replace('.', '')
        if ap.startswith('p') and hour < 12:
            return hour + 12
        if ap.startswith('a') and hour == 12:
            return 0
        return hour
    if hour > 12 or hour == 0:
        return hour
    if part in ('evening', 'avond', 'avonds', 'night', 'nacht', 'afternoon', 'middag', 'middags') and hour < 12:
        return hour + 12 if not (part in ('night', 'nacht') and hour >= 10) else hour
    if part in ('morning', 'ochtend', 'ochtends'):
        return hour
    if prefer == 'am':                          # alarms and repeating reminders: 7 means 07:00, 3 means 15:00
        return hour + 12 if 1 <= hour <= 4 else hour
    if has_day and near_day:                    # "tomorrow at 6" -> early hours read as afternoon/evening
        return hour + 12 if 1 <= hour <= 6 else hour
    # bare "at 6": the next time it will be 6 o'clock
    am = now_dt.replace(hour=hour % 12, minute=minute, second=0, microsecond=0)
    if am > now_dt + timedelta(seconds=20):
        return hour % 12
    return (hour % 12) + 12


def extract_time(text, now=None, prefer='next'):
    """Find a point in time in `text`. Returns (aware datetime, remaining_text) or None."""
    now = now or local_now()
    work = text
    spans = []

    def cut(match):
        spans.append(match.span())

    # 1) "in 10 minutes", "over een half uur"
    match = REL_RE.search(work)
    if match:
        delta = parse_duration(match.group('d'))
        if delta:
            cut(match)
            return now + delta, _remove(work, spans)
    # 2) full ISO date/time
    match = ISO_RE.search(work)
    if match:
        hour, minute = (int(match.group('h')), int(match.group('mi'))) if match.group('h') else (9, 0)
        try:
            when = datetime(int(match.group('y')), int(match.group('mo')), int(match.group('d')), hour, minute, tzinfo=now.tzinfo)
        except ValueError:
            when = None
        if when:
            cut(match)
            return when, _remove(work, spans)
    # 3) calendar day: 12 october / october 12 / 12-10
    base_date = None
    named = DATE_NAMED_RE.search(work)
    if named:
        day = int(named.group('d1') or named.group('d2'))
        month = MONTHS[(named.group('m1') or named.group('m2')).lower()]
        year = now.year
        try:
            candidate = datetime(year, month, day, tzinfo=now.tzinfo)
            if candidate.date() < now.date():
                candidate = candidate.replace(year=year + 1)
            base_date = candidate.date()
            cut(named)
        except ValueError:
            pass
    if base_date is None:
        dmy = DMY_RE.search(work)
        if dmy and not re.search(r'\d{1,2}[/-]\d{1,2}[/-]\d', work) or (dmy and dmy.group('y')):
            try:
                year = int(dmy.group('y')) if dmy.group('y') else now.year
                year = year + 2000 if year < 100 else year
                candidate = datetime(year, int(dmy.group('mo')), int(dmy.group('d')), tzinfo=now.tzinfo)
                if not dmy.group('y') and candidate.date() < now.date():
                    candidate = candidate.replace(year=year + 1)
                base_date = candidate.date()
                cut(dmy)
            except (ValueError, AttributeError):
                pass
    # 4) day words
    day_word = None
    day_match = DAY_RE.search(work)
    if base_date is None and day_match:
        day_word = day_match.group('day').lower()
        cut(day_match)
    elif base_date is None:
        nw = NEXT_WEEK_RE.search(work)
        if nw:
            base_date = (now + timedelta(days=7)).date()
            cut(nw)
    # 5) part of day
    part_match = PART_RE.search(work)
    part = None
    if part_match and not _inside(part_match.span(), spans):
        part = part_match.group('part').lower()
        cut(part_match)
    # 6) clock time (only when clearly a clock reading)
    hour = minute = None
    ap = None
    for tm in TIME_RE.finditer(work):
        if _inside(tm.span(), spans):
            continue
        h, m, a, pre = int(tm.group('h')), tm.group('m'), tm.group('ap'), tm.group('pre')
        explicit = bool(pre) or m is not None or bool(a)
        if not explicit or h > 24 or (m is not None and int(m) > 59):
            continue
        hour, minute, ap = h % 24 if h == 24 else h, int(m or 0), a
        cut(tm)
        break
    if hour is None and base_date is None and day_word is None and part is None:
        return None
    # ---- assemble
    has_day = day_word is not None or base_date is not None
    tonight = day_word in ('tonight', 'vanavond', 'vannacht')
    if base_date is None:
        base_date = now.date()
        if day_word in ('tomorrow', 'tmrw', 'morgen'):
            base_date = (now + timedelta(days=1)).date()
        elif day_word in ('day after tomorrow', 'overmorgen'):
            base_date = (now + timedelta(days=2)).date()
        elif tonight:
            part = part or ('night' if day_word == 'vannacht' else 'evening')
        elif day_word and day_word not in ('today', 'vandaag'):
            base_date = (now + timedelta(days=(WEEKDAYS[day_word.split()[-1]] - now.weekday()) % 7)).date()
    near_day = day_word not in (None, 'today', 'vandaag')
    if hour is None:
        hour = 20 if (tonight and day_word != 'vannacht' and part == 'evening') else (DAY_PARTS.get(part, 9) if part else 9)
        minute = 0
    else:
        hour = _hour_candidates(hour, minute, ap, near_day or base_date != now.date(), now, has_day, part, prefer)
    when = datetime(base_date.year, base_date.month, base_date.day, hour % 24, minute, tzinfo=now.tzinfo)
    if when <= now + timedelta(seconds=5):
        if day_word and day_word.split()[-1] in WEEKDAYS:
            when += timedelta(days=7)           # "friday" said on friday after the time has passed -> next friday
        elif not has_day:
            when += timedelta(days=1)           # a bare clock time that passed today means tomorrow
    return when, _remove(work, spans)


def _inside(span, spans):
    return any(span[0] >= s[0] and span[1] <= s[1] for s in spans)


def _remove(text, spans):
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + ' ' + text[end:]
    return re.sub(r'\s+', ' ', text).strip()


# ------------------------------------------------------------------ repeats

REPEAT_PATTERNS = [
    (re.compile(r'\b(?:every\s+)?(?:weekdays|every\s+weekday|elke\s+werkdag|op\s+werkdagen)\b', re.I), lambda m: 'weekdays'),
    (re.compile(rf'\b(?:every|elke|iedere|ieder)\s+(?P<wd>{_WD})\b', re.I), lambda m: f"weekly:{WEEKDAYS[m.group('wd').lower()]}"),
    (re.compile(r'\b(?:every\s+day|everyday|daily|elke\s+dag|iedere\s+dag|dagelijks)\b', re.I), lambda m: 'daily'),
    (re.compile(r'\b(?:every\s+week|weekly|elke\s+week|iedere\s+week|wekelijks)\b', re.I), lambda m: 'weekly'),
    (re.compile(r'\b(?:every\s+month|monthly|elke\s+maand|iedere\s+maand|maandelijks)\b', re.I), lambda m: 'monthly'),
    (re.compile(rf'\b(?:every|elke|iedere|om\s+de)\s+(?P<n>{_NUM})\s*(?P<u>{_UNIT})\b', re.I),
     lambda m: f"every:{max(1, int(_number(m.group('n')) * UNITS[m.group('u').lower()] // 60))}"),
]


def extract_repeat(text):
    """Return (repeat_spec, remaining_text) or ('', text)."""
    for pattern, build in REPEAT_PATTERNS:
        match = pattern.search(text)
        if match:
            spec = build(match)
            return spec, _remove(text, [match.span()])
    return '', text


def next_occurrence(due, repeat, now=None):
    """The next due time after `due` (and after `now`) for a repeating reminder."""
    now = now or local_now()
    kind, _, arg = repeat.partition(':')
    due = due.astimezone(now.tzinfo)
    guard = 0
    while guard < 4000:
        guard += 1
        if kind == 'every':
            due = due + timedelta(minutes=int(arg or 60))
        elif kind == 'daily':
            due = due + timedelta(days=1)
        elif kind == 'weekdays':
            due = due + timedelta(days=1)
            while due.weekday() >= 5:
                due = due + timedelta(days=1)
        elif kind == 'weekly':
            due = due + timedelta(days=7)
        elif kind == 'monthly':
            month = due.month % 12 + 1
            year = due.year + (1 if due.month == 12 else 0)
            day = min(due.day, 28)
            due = due.replace(year=year, month=month, day=day)
        else:
            return None
        if due > now:
            return due
    return None


def describe_repeat(repeat):
    kind, _, arg = (repeat or '').partition(':')
    if kind == 'daily':
        return 'every day'
    if kind == 'weekdays':
        return 'every weekday'
    if kind == 'weekly':
        return f'every {WEEKDAY_NAMES[int(arg)]}' if arg else 'every week'
    if kind == 'monthly':
        return 'every month'
    if kind == 'every':
        minutes = int(arg or 60)
        return f'every {minutes // 60} h' if minutes % 60 == 0 and minutes >= 60 else f'every {minutes} min'
    return ''


def humanize(due, now=None, with_relative=True):
    now = now or local_now()
    due = due.astimezone(now.tzinfo)
    seconds = int((due - now).total_seconds())
    if with_relative and 0 <= seconds < 3600:
        if seconds < 90:
            return f'in {max(seconds, 1)} sec' if seconds < 60 else 'in 1 min'
        return f'in {round(seconds / 60)} min'
    clock = due.strftime('%H:%M')
    if due.date() == now.date():
        return f'today at {clock}'
    if due.date() == (now + timedelta(days=1)).date():
        return f'tomorrow at {clock}'
    if 0 < (due.date() - now.date()).days < 7:
        return f'{WEEKDAY_NAMES[due.weekday()]} at {clock}'
    return due.strftime('%a %d %b at ') + clock


def format_duration(delta):
    seconds = int(round(delta.total_seconds()))
    parts = []
    for label, size in (('h', 3600), ('min', 60), ('sec', 1)):
        if seconds >= size:
            parts.append(f'{seconds // size} {label}')
            seconds %= size
    return ' '.join(parts) or '0 sec'


# ------------------------------------------------------------------ storage

def create(kind, title, due, repeat='', source='user', notes=''):
    title = ' '.join(str(title).split())[:200] or ('Timer' if kind == 'timer' else 'Reminder')
    rid = db.execute('INSERT INTO reminders(kind,title,notes,due_at,repeat,status,source,fired_at,created_at) VALUES(?,?,?,?,?,\'active\',?,\'\',?)',
                     (kind, title, notes, to_utc(due), repeat, source, db.now()))
    events.publish(f'{kind}.created', f"{'Timer' if kind == 'timer' else 'Reminder'} set: {title}", humanize(due), 'info',
                   {'id': rid, 'due_at': to_utc(due)})
    return rid


def get(rid):
    rows = db.rows('SELECT * FROM reminders WHERE id=?', (int(rid),))
    return rows[0] if rows else None


def active():
    return db.rows("SELECT * FROM reminders WHERE status='active' ORDER BY due_at,id")


def waiting():
    """Fired and not yet acknowledged: these stay on screen until dismissed or snoozed."""
    return db.rows("SELECT * FROM reminders WHERE status='fired' ORDER BY fired_at,id")


def done(rid):
    row = get(rid)
    if not row:
        raise ValueError('Reminder not found')
    db.execute("UPDATE reminders SET status='done' WHERE id=?", (row['id'],))
    events.publish('reminder.done', f"Done: {row['title']}", '', 'info', {'id': row['id']})
    return row


def cancel(rid):
    row = get(rid)
    if not row:
        raise ValueError('Reminder not found')
    db.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (row['id'],))
    events.publish('reminder.cancelled', f"Cancelled: {row['title']}", '', 'info', {'id': row['id']})
    return row


def snooze(rid, minutes=10):
    row = get(rid)
    if not row:
        raise ValueError('Reminder not found')
    minutes = max(1, min(int(minutes), 24 * 60))
    due = local_now() + timedelta(minutes=minutes)
    db.execute("UPDATE reminders SET status='active',due_at=?,fired_at='' WHERE id=?", (to_utc(due), row['id']))
    events.publish('reminder.snoozed', f"Snoozed {minutes} min: {row['title']}", humanize(due), 'info', {'id': row['id'], 'due_at': to_utc(due)})
    return row, due


def check_due(now=None):
    """Fire everything that is due. Called about once a second."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat(timespec='seconds')
    fired = []
    for row in db.rows("SELECT * FROM reminders WHERE status='active' AND due_at<=? ORDER BY due_at,id", (stamp,)):
        is_timer = row['kind'] == 'timer'
        label = 'Timer finished' if is_timer else ('Alarm' if row['kind'] == 'alarm' else 'Reminder')
        events.publish('timer.done' if is_timer else 'reminder.due', f"{label}: {row['title']}", row['notes'] or '', 'alert',
                       {'id': row['id'], 'kind': row['kind'], 'title': row['title']})
        if row['repeat']:
            following = next_occurrence(from_iso(row['due_at']), row['repeat'], now.astimezone())
            if following:
                db.execute("UPDATE reminders SET due_at=?,fired_at=?,status='active' WHERE id=?", (to_utc(following), db.now(), row['id']))
            else:
                db.execute("UPDATE reminders SET status='fired',fired_at=? WHERE id=?", (db.now(), row['id']))
        else:
            db.execute("UPDATE reminders SET status='fired',fired_at=? WHERE id=?", (db.now(), row['id']))
        fired.append(row)
    return fired


def snapshot(now=None):
    """What clients need to draw timers and alerts."""
    now = now or local_now()
    items = []
    for row in active():
        due = from_iso(row['due_at'])
        items.append({'id': row['id'], 'kind': row['kind'], 'title': row['title'], 'due_at': row['due_at'],
                      'seconds_left': max(0, int((due - now).total_seconds())), 'when': humanize(due, now),
                      'repeat': describe_repeat(row['repeat'])})
    alerts = [{'id': r['id'], 'kind': r['kind'], 'title': r['title'], 'fired_at': r['fired_at']} for r in waiting()]
    return {'active': items, 'alerts': alerts}


# ------------------------------------------------------------------ natural language

_FILLERS = [
    r"^(?:hey |ok(?:ay)? |please |kun je |kunt u |can you |could you |would you |will you |sage[, ]+)+",
    r"\b(?:set|make|create|add|zet|maak|stel)\s+(?:me\s+)?(?:a|an|een|my)?\s*(?:new\s+)?(?:reminder|herinnering|alarm|wekker|timer)s?\b(?:\s+(?:for|to|om|voor|dat|that|about|of))?",
    r"\b(?:remind\s+me|remind\s+us|herinner\s+me|herinner\s+mij|herinner\s+ons)\b(?:\s+(?:eraan|er\s+aan))?(?:\s+(?:to|that|about|of|om|dat|aan))?",
    r"\b(?:reminder|herinnering)\b(?:\s+(?:to|for|that|about|om|voor|dat))?",
    r"\bplease\b|\balsjeblieft\b|\balstublieft\b|\bthanks?\b|\bthank you\b",
]


def clean_title(text):
    for pattern in _FILLERS:
        text = re.sub(pattern, ' ', text, flags=re.I)
    text = re.sub(r'\b(?:to|that|om|dat)\s*$', ' ', text.strip(), flags=re.I)
    text = re.sub(r'^\s*(?:to|that|om|dat|about|of|for|voor|aan)\s+', ' ', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip(' ,.;:!?-')
    return (text[:1].upper() + text[1:]) if text else ''


_SET_TIMER = re.compile(r'\b(?:timer|countdown|aftellen|stopwatch)\b', re.I)
_REMIND = re.compile(r'\b(?:remind\s+(?:me|us)|reminder|herinner\s+(?:me|mij|ons)|herinnering)\b', re.I)
_ALARM = re.compile(r"\b(?:wake\s+me(?:\s+up)?|alarm|wekker|maak\s+me\s+wakker|wek\s+me)\b", re.I)
_LIST = re.compile(r"\b(?:what|which|show|list|any|how\s+many|welke|wat|toon|laat|geef|heb\s+ik|do\s+i\s+have|are\s+there)\b", re.I)
_CANCEL = re.compile(r"\b(?:cancel|delete|remove|clear|stop|annuleer|verwijder|wis|stop)\b", re.I)
_THINGS = re.compile(r"\b(?:timers?|reminders?|alarms?|herinneringen|herinnering|wekkers?|wekker)\b", re.I)
_SNOOZE = re.compile(r"^\s*(?:please\s+)?snooze\b(?P<rest>.*)$|^\s*(?:remind\s+me\s+again|herinner\s+me\s+straks)\b(?P<rest2>.*)$", re.I)
_DISMISS = re.compile(r"^\s*(?:ok(?:ay)?[, ]*)?(?:dismiss|stop\s+(?:the\s+)?(?:alarm|timer|beeping)|got\s+it|i(?:'ve| have)\s+done\s+it|done|gedaan|klaar|ik\s+heb\s+het\s+gedaan|oke(?:e)?\s+bedankt)\b[\s.!]*$", re.I)


def parse_command(text, now=None):
    """Detect a reminder/timer/alarm command in a chat message. Returns a dict or None."""
    now = now or local_now()
    raw = text.strip()
    if not raw or len(raw) > 400:
        return None
    lower = raw.lower()
    if _DISMISS.match(raw) and waiting():
        return {'intent': 'dismiss'}
    snooze_match = _SNOOZE.match(raw)
    if snooze_match and waiting():
        rest = snooze_match.group('rest') or snooze_match.group('rest2') or ''
        delta = parse_duration(rest)
        return {'intent': 'snooze', 'minutes': max(1, int(delta.total_seconds() // 60)) if delta else 10}
    sets_something = bool(re.search(r'\b(?:remind\s+(?:me|us)|herinner\s+(?:me|mij|ons)|set|zet|stel|make|maak|create|start|wake\s+me|wek\s+me)\b', lower))
    if _THINGS.search(lower) and re.search(r'\b(?:cancel|delete|remove|clear|stop|annuleer|verwijder|wis)\b', lower) and not re.search(r'\b(?:remind\s+(?:me|us)|herinner)\b', lower):
        return {'intent': 'cancel', 'text': raw}
    is_question = raw.endswith('?') or bool(re.match(r'^\s*(?:what|which|show|list|how many|welke|wat|toon|laat|geef|heb|do i|are there)', lower))
    plural_or_mine = bool(re.search(r'\b(?:timers|reminders|alarms|herinneringen|wekkers)\b|\b(?:my|mijn)\s+(?:timer|reminder|alarm|herinnering|wekker)\b|\bdo\s+i\s+have\b|\bheb\s+ik\b', lower))
    if _THINGS.search(lower) and is_question and plural_or_mine and not sets_something:
        return {'intent': 'list'}
    if _SET_TIMER.search(lower) and not is_question:
        delta = parse_duration(raw)
        if delta is None:
            return {'intent': 'timer_missing'}
        rest = DURATION_RE.sub(' ', raw)
        rest = re.sub(r"\b(?:set|start|create|begin|make|zet|maak|stel|a|an|een|me|the|timer|countdown|aftellen|for|of|van|voor|op|on|please|can|you|could|kun|je|u|hey|ok|okay|sage|my|mijn)\b", ' ', rest, flags=re.I)
        label = re.sub(r'\s+', ' ', rest).strip(' ,.;:!?-')
        return {'intent': 'timer', 'delta': delta, 'label': label[:1].upper() + label[1:] if label else ''}
    wants_reminder = _REMIND.search(lower) and not is_question
    wants_alarm = _ALARM.search(lower) and not is_question and not wants_reminder
    if wants_reminder or wants_alarm:
        repeat, rest = extract_repeat(raw)
        prefer = 'am' if (wants_alarm or repeat) else 'next'
        found = extract_time(rest, now, prefer)
        title_source = rest
        if found:
            when, title_source = found
            if repeat.startswith('weekly:'):                       # "every monday at 9": first one is the next monday
                target = int(repeat.split(':')[1])
                when = when.replace(year=now.year, month=now.month, day=now.day) + timedelta(days=(target - now.weekday()) % 7)
                if when <= now:
                    when += timedelta(days=7)
            elif repeat == 'weekdays':
                while when.weekday() >= 5:
                    when += timedelta(days=1)
        elif repeat.startswith('every:'):                           # "every 2 hours" starts one interval from now
            when = now + timedelta(minutes=int(repeat.split(':')[1]))
        else:
            return {'intent': 'reminder_missing_time', 'title': clean_title(rest) if wants_reminder else 'Alarm'}
        if wants_alarm:
            title = clean_title(re.sub(r"\b(?:wake\s+me(?:\s+up)?|set|an|a|alarm|wekker|zet|een|maak\s+me\s+wakker|wek\s+me|for|voor|at|om)\b", ' ', title_source, flags=re.I))
            return {'intent': 'alarm', 'when': when, 'title': title or 'Wake up', 'repeat': repeat}
        title = clean_title(title_source)
        if title.lower() in ('for', 'at', 'om', 'voor', 'to', 'dat'):
            title = ''
        return {'intent': 'reminder', 'when': when, 'title': title, 'repeat': repeat}
    return None


def handle(text, now=None):
    """Run a reminder/timer command. Returns the reply text, or None when the message is not one."""
    now = now or local_now()
    command = parse_command(text, now)
    if not command:
        return None
    intent = command['intent']
    if intent == 'timer':
        due = now + command['delta']
        label = command['label']
        create('timer', label or f"{format_duration(command['delta'])} timer", due, source='chat')
        return f"Timer set for {format_duration(command['delta'])}" + (f" ({label})" if label else '') + f". It will ring at {due.strftime('%H:%M:%S')}."
    if intent == 'timer_missing':
        return 'How long should the timer run? For example: "set a timer for 10 minutes".'
    if intent in ('reminder', 'alarm'):
        title = command['title'] or ('Alarm' if intent == 'alarm' else 'Reminder')
        due = command['when']
        if due <= now:
            return f"That time ({due.strftime('%a %d %b %H:%M')}) has already passed. Tell me a time in the future."
        create('alarm' if intent == 'alarm' else 'reminder', title, due, command.get('repeat', ''), 'chat')
        repeat = describe_repeat(command.get('repeat', ''))
        verb = 'I will wake you' if intent == 'alarm' and title == 'Wake up' else f'I will remind you: "{title}"'
        return f"{verb} {humanize(due, now, with_relative=False)}" + (f", {repeat}" if repeat else '') + '.'
    if intent == 'reminder_missing_time':
        return 'When should I remind you? For example: "remind me to call mum tomorrow at 18:00" or "in 20 minutes".'
    if intent == 'list':
        items = active()
        waiting_items = waiting()
        if not items and not waiting_items:
            return 'You have no active timers or reminders.'
        lines = []
        for row in waiting_items:
            lines.append(f"- ringing now: {row['title']}")
        for row in items:
            when = humanize(from_iso(row['due_at']), now)
            tag = 'timer' if row['kind'] == 'timer' else ('alarm' if row['kind'] == 'alarm' else 'reminder')
            repeat = describe_repeat(row['repeat'])
            lines.append(f"- {row['title']} ({tag}): {when}" + (f", {repeat}" if repeat else ''))
        return 'Your timers and reminders:\n' + '\n'.join(lines)
    if intent == 'cancel':
        return _cancel_from_text(command['text'])
    if intent == 'dismiss':
        rows = waiting()
        for row in rows:
            done(row['id'])
        return 'Okay, dismissed.' if rows else 'Nothing is ringing right now.'
    if intent == 'snooze':
        rows = waiting()
        if not rows:
            return 'Nothing is ringing right now, so there is nothing to snooze.'
        for row in rows:
            snooze(row['id'], command['minutes'])
        return f"Snoozed for {command['minutes']} minutes."
    return None


def _cancel_from_text(text):
    lower = text.lower()
    rows = active() + waiting()
    if not rows:
        return 'You have no active timers or reminders to cancel.'
    wanted = []
    id_match = re.search(r'#\s*(\d+)', lower)
    if id_match:
        wanted = [r for r in rows if r['id'] == int(id_match.group(1))]
    elif re.search(r'\b(?:all|every|alle|alles)\b', lower):
        wanted = [r for r in rows if ('timer' not in lower or r['kind'] == 'timer') and (not re.search(r'reminders?|herinnering', lower) or r['kind'] != 'timer')]
    else:
        kind = 'timer' if re.search(r'timer', lower) else ('alarm' if re.search(r'alarm|wekker', lower) else None)
        words = {w for w in re.findall(r'[a-zà-ÿ]{4,}', lower)} - {'cancel', 'delete', 'remove', 'timer', 'timers', 'reminder', 'reminders', 'alarm', 'alarms', 'annuleer', 'verwijder', 'herinnering', 'wekker', 'please', 'stop', 'clear', 'that', 'the'}
        pool = [r for r in rows if kind is None or r['kind'] == kind]
        titled = [r for r in pool if words and any(w in r['title'].lower() for w in words)]
        wanted = titled or ([pool[0]] if len(pool) == 1 else [])
        if not wanted and pool:
            soonest = sorted(pool, key=lambda r: r['due_at'])[0]
            wanted = [soonest]
    if not wanted:
        return 'I could not find that one. Say "what timers do I have" to see them.'
    for row in wanted:
        cancel(row['id'])
    names = ', '.join(r['title'] for r in wanted[:4])
    return f"Cancelled: {names}." if len(wanted) < 5 else f'Cancelled {len(wanted)} items.'
