"""Sage voice gateway: lets a small voice device (the Waveshare ESP32-S3-AUDIO-Board) talk to Sage over Wi-Fi.

The device does the wake word, echo cancellation and silence detection. Sage does the heavy work on the PC:
speech-to-text (faster-whisper), the normal chat pipeline (agent.respond, so voice and chat share history, memory,
tools and the reminder commands), and text-to-speech (Piper, espeak-ng or Windows voices).

Everything is plain HTTP on its own port (SAGE_VOICE_PORT, default 8002). Audio is always 16 kHz, mono, signed
16-bit little-endian PCM, with no WAV header (a WAV file is also accepted when uploading).

    POST /api/voice/ask        body = one spoken utterance (Content-Length required, at most ~30 s)
                               -> 204 when nothing was understood, otherwise 200 + the spoken reply as chunked PCM.
                               Headers: X-Sage-Heard, X-Sage-Reply (URL-encoded, shortened), X-Sage-Lang,
                               X-Sage-Listen (1 = open the microphone again without a wake word),
                               X-Sage-Asleep (1 = sleep mode: lights and screen off).
    GET  /api/voice/poll?wait=25   long-poll for things Sage says by itself (morning brief, timers, reminders,
                               alarms). 204 after `wait` seconds, otherwise the same chunked PCM plus
                               X-Sage-Kind (brief|timer|reminder|alarm|say) and X-Sage-Id. Every answer, also the 204,
                               carries X-Sage-Asleep.
    POST /api/voice/button     {"press": "short"|"long"}  physical button: short approves the one request waiting
                               for a button press, long cancels it. 204 when nothing is waiting.
    POST /api/voice/ring       {"state": "listening"|"idle"|"muted"|"error"}  what the device is doing right now
                               (shown as the light ring on the wall display).
    POST /api/voice/say        {"text": "...", "lang": "nl"}  make the speaker say something.
    GET  /api/voice/status     engines, sleep mode, device online.
    GET  /phone                the Talk page for the iPhone (needs HTTPS: Tailscale Serve).
    POST /api/voice/phone/ask|text|button   the phone as a second speaker. Needs SAGE_PHONE_TOKEN (Bearer) on every
                               request, even from this PC, and never works without it. Same limits as the speaker.

Security (see VOICE-GUIDE.md):
  * client IP must be this PC or in SAGE_ALLOWED_CLIENT_IPS;
  * with SAGE_VOICE_SECRET set every request must carry X-Sage-Time and X-Sage-Sig (HMAC, see voice_util.sign),
    otherwise SAGE_VOICE_TOKEN (X-Sage-Token) is checked when set;
  * 5 refused requests within 10 minutes lock that address for 10 minutes and raise a warning on the wall display;
  * voice may only run the tools in trust.VOICE_ALLOWED, everything else needs approval (dashboard, or the button
    for tools that are normally safe); every request is recorded in the voice_audit table.

Command line checks (run from the Sage folder):
    python -m sage.voice --check
    python -m sage.voice --say "Good morning" --lang en        (writes data/voice-test.wav)
    python -m sage.voice --wav some.wav                        (prints what Sage heard)
"""
import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import config, db, events, reminders, trust
from . import voice_util as vu

MAX_UPLOAD = 1_100_000            # a little over 30 seconds of 16 kHz mono audio
_stop = threading.Event()
_server = None
_last_poll = 0.0
_brief_spoken_at = 0.0
_tts_errors = {}


def _log(where, exc):
    try:
        db.log_action('voice.' + where, {}, 'error', str(exc)[:300])
    except Exception:
        pass
    print(f'Sage voice [{where}]: {exc}', file=sys.stderr)

# ------------------------------------------------------------------ audit trail

_audit_ready = False
_audit_count = 0


def _audit_init():
    global _audit_ready
    if not _audit_ready:
        db.execute('CREATE TABLE IF NOT EXISTS voice_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, outcome TEXT, ip TEXT, heard TEXT, reply TEXT, detail TEXT)')
        _audit_ready = True


def audit(kind, outcome, ip='', heard='', reply='', detail=''):
    """One row per voice event: who (address), what was heard, what Sage did, and what was refused."""
    global _audit_count
    try:
        _audit_init()
        db.execute('INSERT INTO voice_audit(at,kind,outcome,ip,heard,reply,detail) VALUES(?,?,?,?,?,?,?)',
                   (db.now(), kind, outcome, ip, str(heard)[:300], str(reply)[:300], str(detail)[:300]))
        _audit_count += 1
        if _audit_count % 200 == 0:
            db.execute('DELETE FROM voice_audit WHERE id < (SELECT MAX(id) FROM voice_audit) - 3000')
    except Exception as exc:
        print(f'Sage voice [audit]: {exc}', file=sys.stderr)


def audit_rows(limit=50):
    _audit_init()
    return db.rows('SELECT id,at,kind,outcome,ip,heard,reply,detail FROM voice_audit ORDER BY id DESC LIMIT ?', (max(1, min(int(limit), 500)),))


def audit_summary():
    """Counts for the last 24 hours."""
    _audit_init()
    cutoff = (datetime.now().astimezone() - timedelta(hours=24)).isoformat(timespec='seconds')
    rows = db.rows('SELECT kind,outcome,COUNT(*) AS n FROM voice_audit WHERE at>=? GROUP BY kind,outcome', (cutoff,))
    total = lambda kind, outcome=None: sum(r['n'] for r in rows if r['kind'] == kind and (outcome is None or r['outcome'] == outcome))
    return {'requests': total('ask'), 'held_for_approval': total('ask', 'held'), 'refused_logins': total('auth'), 'lockouts': total('auth', 'locked'),
            'button_approvals': total('button', 'approved'), 'announcements': total('announce')}

# ------------------------------------------------------------------ state: sleep mode and the light ring

_state_cond = threading.Condition()
_state_version = 0
_state_last = None
_busy = {'thinking': 0, 'speaking': 0}
_busy_lock = threading.Lock()
_device_ring = ''
_ringing = False
_error_until = 0.0
_asleep = False


def online():
    return bool(_last_poll and time.time() - _last_poll < 60)


def _effective_ring():
    if _error_until > time.time():
        return 'error'
    if _busy['speaking'] > 0:
        return 'speaking'
    if _busy['thinking'] > 0:
        return 'thinking'
    if _device_ring in ('listening', 'muted'):
        return _device_ring
    if _ringing:
        return 'alert'
    return 'sleeping' if _asleep else 'idle'


def _publish_state():
    global _state_version, _state_last
    snap = (_effective_ring(), _asleep, online())
    with _state_cond:
        if snap != _state_last:
            _state_last = snap
            _state_version += 1
            _state_cond.notify_all()


def state_snapshot():
    return {'version': _state_version, 'ring': _effective_ring(), 'asleep': _asleep, 'device_online': online()}


def wait_state(since, wait):
    """Long-poll used by the wall display: returns as soon as the ring colour or sleep mode changes."""
    deadline = time.monotonic() + max(0.0, wait)
    with _state_cond:
        while _state_version <= since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _state_cond.wait(min(remaining, 5.0))
    return state_snapshot()


def _bump(name, delta):
    with _busy_lock:
        _busy[name] = max(0, _busy[name] + delta)
    _publish_state()


def _load_sleep():
    global _asleep
    try:
        rows = db.rows("SELECT value FROM settings WHERE key='voice_sleep'")
        _asleep = bool(rows and json.loads(rows[0]['value']))
    except Exception:
        _asleep = False


def set_sleep(asleep):
    global _asleep
    _asleep = bool(asleep)
    try:
        db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('voice_sleep', json.dumps(_asleep), db.now()))
    except Exception as exc:
        _log('sleep', exc)
    with _ann_cond:
        _ann_cond.notify_all()
    _publish_state()

# ------------------------------------------------------------------ speech to text

_stt_model = None
_stt_lock = threading.Lock()


def stt_available():
    try:
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def _load_stt():
    global _stt_model
    if _stt_model is None:
        from faster_whisper import WhisperModel
        compute = 'int8' if config.VOICE_STT_DEVICE == 'cpu' else 'float16'
        _stt_model = WhisperModel(config.VOICE_STT_MODEL, device=config.VOICE_STT_DEVICE, compute_type=compute)
    return _stt_model


def _best_allowed_language(info):
    allowed = config.VOICE_LANGS or ['en']
    probs = getattr(info, 'all_language_probs', None)
    if probs:
        ranked = [lang for lang, _ in sorted(probs, key=lambda item: -item[1]) if lang in allowed]
        if ranked:
            return ranked[0]
    return allowed[0]


def transcribe(pcm):
    """16 kHz mono s16le PCM -> (text, language code). Raises RuntimeError when faster-whisper is not installed."""
    if not stt_available():
        raise RuntimeError('Speech recognition is not installed. Run: pip install faster-whisper')
    import numpy as np
    audio = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype='<i2').astype(np.float32) / 32768.0
    with _stt_lock:
        model = _load_stt()
        options = dict(beam_size=1, vad_filter=True, condition_on_previous_text=False)
        segments, info = model.transcribe(audio, language=None, **options)
        text = ' '.join(s.text.strip() for s in segments).strip()
        lang = info.language
        if config.VOICE_LANGS and lang not in config.VOICE_LANGS:
            lang = _best_allowed_language(info)
            segments, info = model.transcribe(audio, language=lang, **options)
            text = ' '.join(s.text.strip() for s in segments).strip()
    return text, lang

# ------------------------------------------------------------------ text to speech

def _tts_piper(text, lang):
    voice = config.VOICE_PIPER_VOICES.get(lang) or ''
    if not voice or not os.path.isfile(voice):
        return None
    rate = 22050
    try:
        with open(voice + '.json', encoding='utf-8') as handle:
            rate = int(json.load(handle)['audio']['sample_rate'])
    except (OSError, ValueError, KeyError):
        pass
    command = shlex.split(config.VOICE_PIPER_CMD, posix=(os.name != 'nt')) + ['--model', voice, '--output-raw']
    result = subprocess.run(command, input=(text + '\n').encode('utf-8'), capture_output=True, timeout=90, check=False)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError('piper failed: ' + result.stderr.decode('utf-8', 'replace')[-200:])
    return vu.resample(result.stdout, rate)


def _tts_espeak(text, lang):
    exe = shutil.which('espeak-ng') or shutil.which('espeak')
    if not exe:
        return None
    result = subprocess.run([exe, '-v', lang, '-s', '165', '--stdout', text], capture_output=True, timeout=60, check=False)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError('espeak failed: ' + result.stderr.decode('utf-8', 'replace')[-200:])
    return vu.parse_audio(result.stdout)


_SAPI = r'''
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $env:SAGE_TTS_LANG } | Select-Object -First 1
if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }
$s.SetOutputToWaveFile($env:SAGE_TTS_OUT)
$s.Speak($env:SAGE_TTS_TEXT)
$s.Dispose()
'''


def _tts_sapi(text, lang):
    if os.name != 'nt':
        return None
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    try:
        env = dict(os.environ, SAGE_TTS_LANG=lang, SAGE_TTS_OUT=path, SAGE_TTS_TEXT=text)
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', _SAPI],
                                capture_output=True, timeout=60, check=False, env=env)
        if result.returncode != 0:
            raise RuntimeError('windows speech failed: ' + result.stderr.decode('utf-8', 'replace')[-200:])
        with open(path, 'rb') as handle:
            return vu.parse_audio(handle.read())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


ENGINES = (('piper', _tts_piper), ('espeak', _tts_espeak), ('windows', _tts_sapi))


def tts_engines():
    names = []
    if any(path and os.path.isfile(path) for path in config.VOICE_PIPER_VOICES.values()):
        names.append('piper')
    if shutil.which('espeak-ng') or shutil.which('espeak'):
        names.append('espeak')
    if os.name == 'nt':
        names.append('windows')
    return names


def tts(text, lang='en'):
    """Text -> 16 kHz mono s16le PCM using the first engine that works (Piper, espeak-ng, Windows speech)."""
    lang = lang if lang in ('nl', 'en') else (config.VOICE_LANGS or ['en'])[0]
    errors = []
    for name, engine in ENGINES:
        try:
            pcm = engine(text, lang)
        except Exception as exc:
            errors.append(f'{name}: {exc}')
            _tts_errors[name] = str(exc)
            continue
        if pcm:
            return pcm
    raise RuntimeError('No text-to-speech engine worked. Install Piper (see VOICE-GUIDE.md). ' + '; '.join(errors))

# ------------------------------------------------------------------ things Sage says by itself

_ann_cond = threading.Condition()
_ann = []
_ann_seq = 0

_WORDS = {
    'timer': {'en': 'Your timer is done: {t}.', 'nl': 'Je timer is af: {t}.'},
    'reminder': {'en': 'Reminder: {t}.', 'nl': 'Herinnering: {t}.'},
    'alarm': {'en': 'Alarm: {t}.', 'nl': 'Wekker: {t}.'},
    'wake': {'en': 'Good morning. Time to wake up.', 'nl': 'Goedemorgen. Tijd om op te staan.'},
}
URGENT = ('alarm', 'timer')          # still spoken in sleep mode; everything else is held until you wake the speaker


def announce(text, kind='say', lang=None, listen=False, ttl=900, max_chars=None):
    """Queue something for the speaker to say the next time it polls. `text` may be Markdown; it is cleaned for speech.
    In sleep mode only alarms and timers are spoken; the rest waits (up to 10 hours) until you say good morning."""
    global _ann_seq
    lang = lang or vu.guess_lang(text, config.VOICE_DEFAULT_LANG)
    spoken = vu.speech_text(text, lang=lang, max_chars=max_chars or config.VOICE_MAX_CHARS)
    if not spoken:
        return 0
    with _ann_cond:
        _ann_seq += 1
        _ann.append({'id': _ann_seq, 'kind': kind, 'text': spoken, 'lang': lang, 'listen': bool(listen),
                     'expires': time.time() + max(ttl, 10 * 3600 if _asleep and kind not in URGENT else 0)})
        _ann_cond.notify_all()
        return _ann_seq


def next_announcement(wait):
    deadline = time.monotonic() + max(0.0, wait)
    with _ann_cond:
        while True:
            now = time.time()
            _ann[:] = [a for a in _ann if a['expires'] > now]
            for index, item in enumerate(_ann):
                if not _asleep or item['kind'] in URGENT:
                    return _ann.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            _ann_cond.wait(min(remaining, 5.0))


def _speak_brief(text, force=False):
    global _brief_spoken_at
    if not force and time.time() - _brief_spoken_at < 3 * 3600:      # already spoken (for example after the wake-up alarm)
        return
    _brief_spoken_at = time.time()
    announce(text, kind='brief', lang='en', ttl=3 * 3600, max_chars=config.VOICE_BRIEF_MAX_CHARS)


def _on_event(event, nag):
    kind, data = event['kind'], event.get('data') or {}
    title = str(data.get('title') or '').strip()
    lang = vu.guess_lang(title, config.VOICE_DEFAULT_LANG) if title else config.VOICE_DEFAULT_LANG
    if kind == 'user.awake':
        if _asleep:
            set_sleep(False)                                           # the phone's wake signal wakes the speaker too
    elif kind == 'timer.done':
        announce(_WORDS['timer'][lang].format(t=title or '...'), 'timer', lang, listen=True)
        nag[data.get('id')] = [0, time.time(), 'timer', title, lang]
    elif kind == 'reminder.due':
        if data.get('kind') == 'alarm':
            if _asleep:
                set_sleep(False)                                       # an alarm wakes the speaker
            if title.lower() in ('wake up', 'alarm', ''):
                announce(_WORDS['wake'][lang], 'alarm', lang, listen=False)
            else:
                announce(_WORDS['alarm'][lang].format(t=title), 'alarm', lang, listen=False)
            if config.VOICE_ALARM_BRIEF:
                try:
                    from . import scheduler
                    _speak_brief(scheduler.build_brief())
                except Exception as exc:
                    _log('brief', exc)
        else:
            announce(_WORDS['reminder'][lang].format(t=title or '...'), 'reminder', lang, listen=True)
        nag[data.get('id')] = [0, time.time(), data.get('kind') or 'reminder', title, lang]
    elif kind == 'brief.ready' and str(event.get('title', '')).startswith('Your morning brief'):
        text = (data.get('text') or '').strip()
        if text:
            _speak_brief(text)


def _nag(nag):
    """Repeat a timer/reminder every minute (three more times) until someone says done or snooze."""
    global _ringing
    try:
        ringing = {row['id'] for row in reminders.waiting()}
    except Exception:
        return
    _ringing = bool(ringing)
    for rid in list(nag):
        count, last, kind, title, lang = nag[rid]
        if rid not in ringing or count >= 3:
            nag.pop(rid, None)
        elif time.time() - last >= 60:
            nag[rid][0] += 1
            nag[rid][1] = time.time()
            template = _WORDS['timer' if kind == 'timer' else 'reminder'][lang]
            announce(template.format(t=title or '...'), kind if kind in ('timer', 'reminder') else 'reminder', lang, listen=True, ttl=120)


def _announcer():
    since = events.cursor()
    nag = {}
    while not _stop.is_set():
        try:
            for event in events.wait_for(since, 10):
                since = max(since, event['id'])
                _on_event(event, nag)
            _nag(nag)
            _publish_state()                                           # picks up expiries (error colour, device offline)
        except Exception as exc:
            _log('announcer', exc)
            _stop.wait(3)

# ------------------------------------------------------------------ spoken commands (no model needed)

_T = {
    'sleep': {'en': "Goodnight. I'll stay quiet except for alarms and timers.", 'nl': 'Goedenacht. Ik blijf stil, behalve voor wekkers en timers.'},
    'wake': {'en': 'Good morning.', 'nl': 'Goedemorgen.'},
    'nothing': {'en': 'Nothing needs you right now.', 'nl': 'Er wacht niets op je.'},
    'approvals': {'en': '{n} tool request{s} waiting for your approval: {names}.', 'nl': 'Wacht op je goedkeuring ({n}): {names}.'},
    'overdue': {'en': '{n} overdue task{s}: {names}.', 'nl': 'Te laat ({n}): {names}.'},
    'ringing': {'en': 'Ringing now: {names}.', 'nl': 'Gaat nu af: {names}.'},
    'no_timers': {'en': 'You have no active timers or reminders.', 'nl': 'Je hebt geen actieve timers of herinneringen.'},
    'in': {'en': '{t} in {d}', 'nl': '{t} over {d}'},
    'recap': {'en': 'Since earlier: {items}.', 'nl': 'Sinds eerder: {items}.'},
    'no_recap': {'en': 'Nothing notable has happened.', 'nl': 'Er is niets bijzonders gebeurd.'},
    'backup': {'en': 'The latest backup event: {t}.', 'nl': 'De laatste back-up: {t}.'},
    'no_backup': {'en': "I can't find a recent backup event.", 'nl': 'Ik kan geen recente back-up vinden.'},
    'audit': {'en': 'In the last 24 hours the speaker made {requests} requests. {held} were held for approval and {logins} logins were refused.',
              'nl': 'In de afgelopen 24 uur deed de speaker {requests} verzoeken. {held} werden vastgehouden voor goedkeuring en {logins} logins werden geweigerd.'},
}


def _status_needs(lang):
    parts = []
    approvals = db.rows("SELECT tool_name FROM tool_approvals WHERE status='pending' ORDER BY id")
    if approvals:
        names = ', '.join(sorted({a['tool_name'].replace('_', ' ') for a in approvals})[:4])
        n = len(approvals)
        parts.append(_T['approvals'][lang].format(n=n, s='s' if (lang == 'en' and n != 1) else '', names=names))
    today = datetime.now().date().isoformat()
    overdue = db.rows("SELECT title FROM tasks WHERE status!='done' AND due_date IS NOT NULL AND due_date!='' AND due_date<? ORDER BY due_date LIMIT 5", (today,))
    if overdue:
        n = len(overdue)
        parts.append(_T['overdue'][lang].format(n=n, s='s' if (lang == 'en' and n != 1) else '', names=', '.join(o['title'] for o in overdue[:3])))
    ringing = [r['title'] for r in reminders.waiting() if r.get('title')]
    if ringing:
        parts.append(_T['ringing'][lang].format(names=', '.join(ringing[:3])))
    try:
        from . import planner
        extra = str(planner.describe_needs() or '').strip()
        if extra and not re.search(r'\b(nothing|niets)\b', extra, re.I):
            parts.append(extra[:300])
    except Exception:
        pass
    return ' '.join(parts) or _T['nothing'][lang]


def _status_timers(lang):
    rows = db.rows("SELECT kind,title,due_at FROM reminders WHERE status NOT IN ('done','cancelled') ORDER BY due_at LIMIT 6")
    if not rows:
        return _T['no_timers'][lang]
    out = []
    now = reminders.local_now()
    for row in rows:
        title = row['title'] or row['kind']
        try:
            due = datetime.fromisoformat(row['due_at'])
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
            delta = due - now
            if delta.total_seconds() <= 0:
                out.append(title)
            elif delta < timedelta(hours=24):
                out.append(_T['in'][lang].format(t=title, d=reminders.format_duration(delta)))
            else:
                out.append(f"{title}, {due.strftime('%A %H:%M')}")
        except (ValueError, TypeError):
            out.append(title)
    return '. '.join(out) + '.'


def _recent_events(hours=24, limit=200):
    cutoff = (datetime.now().astimezone() - timedelta(hours=hours))
    found = []
    for event in reversed(events.recent(0, limit)):
        if event['kind'].startswith('voice.'):
            continue
        try:
            when = datetime.fromisoformat(event['created_at'])
            if when.tzinfo is None:
                when = when.astimezone()
            if when < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        found.append(event)
    return found


def _status_recap(lang):
    notable = [e for e in _recent_events() if e['level'] in ('success', 'warning', 'alert') or e['kind'].startswith(('job', 'plan', 'task', 'backup'))]
    titles = []
    for event in notable:
        if event['title'] not in titles:
            titles.append(event['title'])
        if len(titles) == 5:
            break
    return _T['recap'][lang].format(items='; '.join(titles)) if titles else _T['no_recap'][lang]


def _status_backup(lang):
    for event in _recent_events(hours=24 * 14, limit=200):
        if 'backup' in (event['kind'] + ' ' + event['title']).lower():
            return _T['backup'][lang].format(t=event['title'])
    return _T['no_backup'][lang]


def _status_audit(lang):
    s = audit_summary()
    return _T['audit'][lang].format(requests=s['requests'], held=s['held_for_approval'], logins=s['refused_logins'])


def intent_reply(intent, lang):
    """Answer a spoken command from Sage's own records. Returns the text to speak."""
    lang = lang if lang in ('nl', 'en') else 'en'
    if intent == 'sleep':
        set_sleep(True)
        return _T['sleep'][lang]
    if intent == 'wake':
        set_sleep(False)
        try:
            from . import scheduler
            _speak_brief(scheduler.build_brief())
        except Exception as exc:
            _log('brief', exc)
        return _T['wake'][lang]
    builder = {'needs': _status_needs, 'timers': _status_timers, 'recap': _status_recap, 'backup': _status_backup, 'audit': _status_audit}[intent]
    return builder(lang)

# ------------------------------------------------------------------ voice approvals by button

_buttons = {'speaker': {'id': None, 'tool': '', 'expires': 0.0}, 'phone': {'id': None, 'tool': '', 'expires': 0.0}}
_button_lock = threading.Lock()
_BUTTON_TEXT = {
    'button': {'en': 'I need your okay to {tool}. Press the button to allow it, or hold it to cancel.',
               'nl': 'Ik heb je akkoord nodig om {tool}. Druk op de knop om toe te staan, of houd ingedrukt om te annuleren.'},
    'phone_button': {'en': 'I need your okay to {tool}. Approve it on the Sage page or the dashboard.',
                     'nl': 'Ik heb je akkoord nodig om {tool}. Keur het goed op de Sage-pagina of het dashboard.'},
    'dashboard': {'en': '{tool} needs your approval on the dashboard.', 'nl': '{tool} heeft je goedkeuring nodig op het dashboard.'},
}


def _max_approval_id():
    rows = db.rows('SELECT COALESCE(MAX(id),0) AS id FROM tool_approvals')
    return rows[0]['id'] if rows else 0


def _new_approvals(since_id):
    return db.rows("SELECT id,tool_name FROM tool_approvals WHERE id>? AND status='pending' ORDER BY id", (since_id,))


def _approval_reply(rows, lang, origin='speaker'):
    """Replace the model's wording when voice asked for something that needs approval. Returns the text to speak.
    A button approval only works for the device that asked: the speaker's button never approves a phone request."""
    from . import tools
    lang = lang if lang in ('nl', 'en') else 'en'
    row = rows[-1]
    pretty = row['tool_name'].replace('_', ' ')
    if tools.base_permission(row['tool_name']) == 'safe':               # only held back because it came by voice
        with _button_lock:
            _buttons[origin].update(id=row['id'], tool=row['tool_name'], expires=time.time() + 300)
        return _BUTTON_TEXT['button' if origin == 'speaker' else 'phone_button'][lang].format(tool=pretty)
    return _BUTTON_TEXT['dashboard'][lang].format(tool=pretty)


def _button_pending(origin):
    slot = _buttons[origin]
    return bool(slot['id'] and slot['expires'] > time.time())


def converse(text, lang, who, origin='speaker'):
    """One turn of conversation, shared by the speaker and the phone: a spoken command, or the normal Sage chat with
    voice's reduced power. Returns (text to speak, language of the reply, outcome, whether to keep listening)."""
    from . import agent, jobs
    held, outcome = [], 'ok'
    intent = vu.match_intent(text)
    try:
        if text.lstrip().startswith('/'):
            answer, outcome = "I don't run slash commands by voice.", 'refused'
        elif intent:
            answer, outcome = intent_reply(intent, lang), 'status' if intent not in ('sleep', 'wake') else intent
        else:
            jobs.note_interaction()
            before = _max_approval_id()
            try:
                with trust.source('voice'):
                    answer = agent.respond(text)
            finally:
                jobs.touch()
            waiting = _new_approvals(before)
            if waiting:
                held, outcome = trust.drain_blocked() or [r['tool_name'] for r in waiting], 'held'
                answer = _approval_reply(waiting, vu.guess_lang(text, lang), origin)
    except Exception as exc:
        _log('chat', exc)
        outcome, answer = 'error', ('Sorry, something went wrong while answering.' if lang == 'en' else 'Sorry, er ging iets mis.')
    reply_lang = vu.guess_lang(answer, lang)
    spoken = vu.speech_text(answer, lang=reply_lang, max_chars=config.VOICE_MAX_CHARS) or ('Done.' if reply_lang == 'en' else 'Klaar.')
    audit('ask', outcome, who, heard=text, reply=spoken, detail=('refused for voice: ' + ', '.join(held)) if held else '')
    return spoken, reply_lang, outcome, (spoken.rstrip().endswith('?') or outcome == 'held')

# ------------------------------------------------------------------ security: lockout and signed requests

_fails = {}
_fails_lock = threading.Lock()
_seen_sigs = {}
LOCK_AFTER, LOCK_WINDOW, LOCK_SECONDS = 5, 600, 600


def _locked(ip):
    with _fails_lock:
        entry = _fails.get(ip)
        return bool(entry and entry[2] > time.time())


def _failed(ip, why):
    now = time.time()
    locked_now = False
    with _fails_lock:
        entry = _fails.get(ip)
        if not entry or now - entry[1] > LOCK_WINDOW:
            entry = _fails[ip] = [0, now, 0.0]
        entry[0] += 1
        if entry[0] >= LOCK_AFTER and entry[2] <= now:
            entry[2] = now + LOCK_SECONDS
            locked_now = True
        if len(_fails) > 500:
            for key in [k for k, v in _fails.items() if v[2] < now and now - v[1] > LOCK_WINDOW]:
                _fails.pop(key, None)
    audit('auth', 'locked' if locked_now else 'refused', ip, detail=why)
    if locked_now:
        try:
            events.publish('voice.security', 'Speaker access blocked', f'Too many refused requests from {ip} ({why}). Blocked for {LOCK_SECONDS // 60} minutes.', 'warning', {'ip': ip})
        except Exception:
            pass


def _clear_failures(ip):
    with _fails_lock:
        _fails.pop(ip, None)

# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'SageVoice/1'
    timeout = 30                      # a stalled or slow connection is dropped instead of tying up a thread

    def log_message(self, fmt, *args):
        pass

    # -- security
    def _proxied(self):
        """True when the request came through a reverse proxy such as Tailscale Serve (which connects from 127.0.0.1)."""
        return any(self.headers.get(name) for name in ('X-Forwarded-For', 'Forwarded', 'X-Real-Ip', 'Tailscale-User-Login', 'Tailscale-User-Name'))

    def _client_id(self):
        """The address used for lockouts. A forwarded address is only believed when the connection really comes from the
        local proxy, and then the last entry (added by the proxy itself) is used, never one the client could have written."""
        peer = self.client_address[0]
        forwarded = [part.strip() for part in (self.headers.get('X-Forwarded-For') or '').split(',') if part.strip()]
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        return forwarded[-1] if (local and forwarded) else peer

    def _who(self):
        return ('phone:' + (self.headers.get('Tailscale-User-Login') or self._client_id()))[:80]

    def _check(self, body):
        """(ok, reason). The address must be allowed; then the signature (or the simple token) must be right."""
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False, 'bad address'
        if address.is_loopback and not self._proxied():
            return True, ''                                   # a program on this PC is already inside Sage's trust boundary
        if str(address) not in config.ALLOWED_CLIENT_IPS:
            return False, 'address not allowed'
        if config.VOICE_SECRET:
            stamp, sig = self.headers.get('X-Sage-Time', ''), self.headers.get('X-Sage-Sig', '').lower()
            try:
                if abs(time.time() - int(stamp)) > 90:
                    return False, 'stale timestamp (check the speaker clock)'
            except ValueError:
                return False, 'missing timestamp'
            expected = vu.sign(config.VOICE_SECRET, stamp, self.command, self.path, body)
            if not hmac.compare_digest(sig, expected):
                return False, 'bad signature'
            now = time.time()
            for key in [k for k, t in _seen_sigs.items() if now - t > 180]:
                _seen_sigs.pop(key, None)
            if sig in _seen_sigs:
                return False, 'replayed request'
            _seen_sigs[sig] = now
        elif config.VOICE_TOKEN:
            sent = self.headers.get('X-Sage-Token', '')
            auth = self.headers.get('Authorization', '')
            if not sent and auth.lower().startswith('bearer '):
                sent = auth[7:].strip()
            if not hmac.compare_digest(sent.encode(), config.VOICE_TOKEN.encode()):
                return False, 'bad token'
        return True, ''

    def _authorize(self, body=b''):
        ip = self._client_id()
        if _locked(ip):
            self._json({'error': 'Too many refused requests. Try again later.'}, 429)
            return False
        ok, why = self._check(body)
        if not ok:
            _failed(ip, why)
            self._json({'error': 'Not allowed'}, 403)
            return False
        _clear_failures(ip)
        return True

    def _authorize_phone(self):
        """The phone only gets in with SAGE_PHONE_TOKEN, also when it connects through the local proxy."""
        who = self._client_id()
        if _locked(who):
            self._json({'error': 'Too many refused requests. Try again later.'}, 429)
            return False
        if not config.PHONE_TOKEN:
            self._json({'error': 'Phone access is off. Set SAGE_PHONE_TOKEN and restart Sage.'}, 503)
            return False
        sent = self.headers.get('X-Sage-Token', '')
        auth = self.headers.get('Authorization', '')
        if not sent and auth.lower().startswith('bearer '):
            sent = auth[7:].strip()
        if not hmac.compare_digest(sent.encode(), config.PHONE_TOKEN.encode()):
            _failed(who, 'bad phone token')
            self._json({'error': 'Not allowed'}, 403)
            return False
        _clear_failures(who)
        return True

    # -- responses
    def _asleep_header(self):
        return {'X-Sage-Asleep': '1' if _asleep else '0'}

    def _send(self, status, body=b'', ctype='application/json; charset=utf-8', headers=None):
        self.send_response(status)
        if status != 204:
            self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body) if status != 204 else 0))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        for key, value in {**self._asleep_header(), **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if body and status != 204:
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False).encode())

    def _stream_speech(self, text, lang, headers, ring=True):
        """Synthesise sentence by sentence and send each as soon as it is ready (chunked transfer)."""
        sentences = vu.split_sentences(text)
        if not sentences:
            return self._send(204)
        if ring:
            _bump('speaking', 1)
        try:
            first = tts(sentences[0], lang)                  # raises before any headers are sent if no engine works
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Sage-Format', 's16le;rate=16000;channels=1')
            for key, value in {**self._asleep_header(), **headers}.items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True

            def push(pcm):
                for start in range(0, len(pcm), 4096):
                    block = pcm[start:start + 4096]
                    self.wfile.write(f'{len(block):x}\r\n'.encode() + block + b'\r\n')
                self.wfile.flush()
            try:
                push(first)
                for sentence in sentences[1:]:
                    push(tts(sentence, lang))
            except (BrokenPipeError, ConnectionError, OSError):
                return
            except Exception as exc:
                _log('tts', exc)
            try:
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except OSError:
                pass
        finally:
            if ring:
                _bump('speaking', -1)

    def _read_body(self):
        try:
            length = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_UPLOAD:
            self._json({'error': 'Body too large or length missing'}, 413)
            return None
        return self.rfile.read(length) if length else b''

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ('/phone', '/phone/'):                    # the page itself holds no secrets; its calls need the token
            from . import voice_phone
            return self._send(200, voice_phone.PAGE.encode('utf-8'), 'text/html; charset=utf-8', {
                'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"})
        if not self._authorize():
            return
        query = parse_qs(url.query)
        try:
            if url.path == '/api/voice/poll':
                return self._poll(query)
            if url.path == '/api/voice/status':
                return self._json(status())
            return self._json({'error': 'Not found'}, 404)
        except Exception as exc:
            _log('get', exc)
            return self._json({'error': str(exc)}, 500)

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return
        path = urlparse(self.path).path
        if path.startswith('/api/voice/phone/'):
            if self._authorize_phone():
                self._phone_post(path, body)
            return
        if not self._authorize(body):
            return
        ip = self.client_address[0]
        try:
            if path == '/api/voice/ask':
                return self._ask(body, ip)
            if path == '/api/voice/button':
                return self._press(json.loads(body or b'{}'), ip)
            if path == '/api/voice/ring':
                global _device_ring
                state = str(json.loads(body or b'{}').get('state', ''))
                _device_ring = state if state in ('listening', 'muted', 'error') else ''
                if state == 'error':
                    _set_error()
                _publish_state()
                return self._send(204)
            if path == '/api/voice/say':
                data = json.loads(body or b'{}')
                text = str(data.get('text', '')).strip()
                if not text:
                    return self._json({'error': 'text is required'}, 400)
                lang = data.get('lang') if data.get('lang') in ('nl', 'en') else None
                audit('say', 'queued', ip, reply=text)
                return self._json({'queued': announce(text, 'say', lang, listen=bool(data.get('listen')))})
            return self._json({'error': 'Not found'}, 404)
        except Exception as exc:
            _log('post', exc)
            return self._json({'error': str(exc)}, 500)

    def _poll(self, query):
        global _last_poll
        _last_poll = time.time()
        _publish_state()
        try:
            wait = max(0.0, min(float((query.get('wait') or ['25'])[0]), 30.0))
        except ValueError:
            wait = 25.0
        item = next_announcement(wait)
        _last_poll = time.time()
        if not item:
            return self._send(204)
        headers = {'X-Sage-Kind': item['kind'], 'X-Sage-Id': str(item['id']), 'X-Sage-Lang': item['lang'],
                   'X-Sage-Listen': '1' if item['listen'] else '0', 'X-Sage-Reply': quote(item['text'][:100])}
        audit('announce', item['kind'], self.client_address[0], reply=item['text'])
        try:
            return self._stream_speech(item['text'], item['lang'], headers)
        except Exception as exc:
            _log('announce', exc)
            _set_error()
            events.publish('voice.failed', 'The speaker could not say something', str(exc)[:200], 'warning')
            return self._send(204)

    def _ask(self, body, ip, origin='speaker'):
        pcm = vu.parse_audio(body)
        if vu.seconds(pcm) < 0.3 or vu.rms(pcm) < 25:
            return self._send(204)
        started = time.time()
        if origin == 'speaker':
            _bump('thinking', 1)
        try:
            try:
                text, lang = transcribe(pcm)
            except Exception as exc:
                _log('stt', exc)
                _set_error()
                audit('ask', 'error', ip, detail='speech recognition: ' + str(exc))
                events.publish('voice.failed', 'Speech recognition failed', str(exc)[:200], 'warning')
                return self._spoken_error('Sorry, I could not recognise speech. Check the Sage voice setup.', 'Sorry, ik kon je niet verstaan.', config.VOICE_DEFAULT_LANG)
            if vu.looks_like_noise(text):
                audit('ask', 'noise', ip, heard=text)
                return self._send(204)
            heard_ms = int((time.time() - started) * 1000)
            spoken, reply_lang, outcome, listen = converse(text, lang, ip, origin)
            limit = 100 if origin == 'speaker' else 600
            headers = {'X-Sage-Heard': quote(text[:limit]), 'X-Sage-Reply': quote(spoken[:limit]), 'X-Sage-Lang': reply_lang,
                       'X-Sage-Listen': '1' if listen else '0', 'X-Sage-Stt-Ms': str(heard_ms)}
            if origin == 'phone':
                headers['X-Sage-Button'] = '1' if _button_pending('phone') else '0'
        finally:
            if origin == 'speaker':
                _bump('thinking', -1)
        try:
            return self._stream_speech(spoken, reply_lang, headers, ring=(origin == 'speaker'))
        except Exception as exc:
            _log('speak', exc)
            _set_error()
            events.publish('voice.failed', 'The speaker could not speak the reply', str(exc)[:200], 'warning')
            return self._json({'error': str(exc), 'reply': spoken}, 500)

    def _press(self, data, ip, origin='speaker'):
        """Approve button. Short press approves the request that is waiting for it, long press cancels it.
        Each device can only answer its own requests."""
        from . import agent
        press = 'long' if data.get('press') == 'long' else 'short'
        with _button_lock:
            slot = _buttons[origin]
            pending = dict(slot) if slot['id'] and slot['expires'] > time.time() else None
            if pending:
                slot.update(id=None, tool='', expires=0.0)
        if not pending:
            audit('button', 'nothing_waiting', ip, detail=press)
            return self._send(204)
        approve = press == 'short'
        try:
            answer = agent.resolve_tool_approval(pending['id'], approve)       # not inside trust.source('voice'): approved by a human
        except ValueError:
            answer = 'That request is no longer waiting.'
        except Exception as exc:
            _log('button', exc)
            answer = 'Sorry, that did not work.'
        audit('button', 'approved' if approve else 'cancelled', ip, reply=answer, detail=pending['tool'])
        lang = vu.guess_lang(answer, config.VOICE_DEFAULT_LANG)
        spoken = vu.speech_text(answer, lang=lang, max_chars=config.VOICE_MAX_CHARS) or ('Done.' if lang == 'en' else 'Klaar.')
        return self._stream_speech(spoken, lang, {'X-Sage-Listen': '0', 'X-Sage-Reply': quote(spoken[:600])}, ring=(origin == 'speaker'))

    def _phone_post(self, path, body):
        who = self._who()
        try:
            action = path.rsplit('/', 1)[-1]
            if action == 'ask':
                return self._ask(body, who, 'phone')
            if action == 'text':
                return self._phone_text(json.loads(body or b'{}'), who)
            if action == 'button':
                return self._press(json.loads(body or b'{}'), who, 'phone')
            return self._json({'error': 'Not found'}, 404)
        except Exception as exc:
            _log('phone', exc)
            return self._json({'error': str(exc)}, 500)

    def _phone_text(self, data, who):
        """Text in, text out: for an iOS Shortcut that uses the iPhone's own dictation and voice."""
        text = str(data.get('text', '')).strip()
        if not text:
            return self._json({'error': 'text is required'}, 400)
        lang = data.get('lang') if data.get('lang') in ('nl', 'en') else vu.guess_lang(text, config.VOICE_DEFAULT_LANG)
        if vu.looks_like_noise(text):
            return self._json({'heard': text, 'reply': '', 'lang': lang, 'listen': False, 'asleep': _asleep, 'button': False, 'outcome': 'noise'})
        spoken, reply_lang, outcome, listen = converse(text[:2000], lang, who, 'phone')
        return self._json({'heard': text, 'reply': spoken, 'lang': reply_lang, 'listen': bool(listen), 'asleep': _asleep,
                           'button': _button_pending('phone'), 'outcome': outcome})

    def _spoken_error(self, english, dutch, lang):
        try:
            return self._stream_speech(dutch if lang == 'nl' else english, lang, {'X-Sage-Listen': '0'})
        except Exception:
            return self._send(500, b'{"error":"voice failure"}')


def _set_error(seconds=15):
    global _error_until
    _error_until = time.time() + seconds
    _publish_state()


def status():
    return {'stt': stt_available(), 'stt_model': config.VOICE_STT_MODEL, 'tts_engines': tts_engines(), 'languages': config.VOICE_LANGS,
            'asleep': _asleep, 'ring': _effective_ring(), 'signed_requests': bool(config.VOICE_SECRET), 'token': bool(config.VOICE_TOKEN),
            'last_poll_seconds_ago': round(time.time() - _last_poll, 1) if _last_poll else None, 'device_online': online(),
            'queued': len(_ann), 'tts_errors': dict(_tts_errors)}


def _warm():
    """Load the speech model in the background so the first spoken request is not slow."""
    if stt_available():
        try:
            with _stt_lock:
                _load_stt()
        except Exception as exc:
            _log('warm', exc)


def start():
    """Start the gateway in background threads. Never raises: a missing speech engine must not stop Sage."""
    global _server
    if not config.VOICE_ENABLED or _server is not None:
        return
    try:
        _audit_init()
        _load_sleep()
        _server = ThreadingHTTPServer((config.HOST, config.VOICE_PORT), Handler)
    except OSError as exc:
        print(f'Sage voice: cannot listen on port {config.VOICE_PORT}: {exc}', file=sys.stderr)
        return
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, name='sage-voice', daemon=True).start()
    threading.Thread(target=_announcer, name='sage-voice-announcer', daemon=True).start()
    threading.Thread(target=_warm, name='sage-voice-warm', daemon=True).start()
    info = status()
    secure = 'signed requests' if info['signed_requests'] else ('token' if info['token'] else 'address allow-list only')
    print(f"Sage voice gateway listening at http://{config.HOST}:{config.VOICE_PORT} ({secure}; speech recognition: {'ready' if info['stt'] else 'NOT installed'}; speech: {', '.join(info['tts_engines']) or 'NONE'})")

# ------------------------------------------------------------------ command line checks

def _cli(argv):
    import argparse
    parser = argparse.ArgumentParser(prog='python -m sage.voice')
    parser.add_argument('--check', action='store_true', help='show which engines are available')
    parser.add_argument('--say', help='synthesise this text to data/voice-test.wav')
    parser.add_argument('--lang', default='en', choices=['en', 'nl'])
    parser.add_argument('--wav', help='transcribe this WAV file')
    args = parser.parse_args(argv)
    if args.say:
        pcm = tts(args.say, args.lang)
        target = config.DATA / 'voice-test.wav'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(vu.wav_bytes(pcm))
        print(f'Wrote {target} ({vu.seconds(pcm):.1f} s). Play it to check the voice.')
    if args.wav:
        with open(args.wav, 'rb') as handle:
            text, lang = transcribe(vu.parse_audio(handle.read()))
        print(f'[{lang}] {text}')
    if args.check or not (args.say or args.wav):
        info = status()
        print(json.dumps(info, indent=2))
        if not info['stt']:
            print('Speech recognition missing: pip install faster-whisper')
        if not info['tts_engines']:
            print('No speech engine found: see VOICE-GUIDE.md (Piper recommended).')


if __name__ == '__main__':
    _cli(sys.argv[1:])
