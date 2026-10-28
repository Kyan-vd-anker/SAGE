"""Pure helpers for Sage's voice gateway: audio conversion and turning chat text into speakable text.

Nothing here imports the rest of Sage, so it can be tested on its own (tests/test_voice.py).
Audio format everywhere on the wire: 16 kHz, mono, signed 16-bit little-endian PCM.
"""
import io
import re
import sys
import wave
from array import array

RATE = 16000

# ------------------------------------------------------------------ audio

def _to_array(pcm):
    samples = array('h')
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == 'big':
        samples.byteswap()
    return samples


def _to_bytes(samples):
    if sys.byteorder == 'big':
        samples = array('h', samples)
        samples.byteswap()
    return samples.tobytes()


def resample(pcm, src, dst=RATE):
    """Linear-interpolation resampler for mono s16le PCM. Good enough for speech."""
    if src == dst or not pcm:
        return pcm
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype='<i2').astype(np.float32)
        n = int(len(x) * dst / src)
        y = np.interp(np.arange(n) * (src / dst), np.arange(len(x)), x)
        return np.clip(y, -32768, 32767).astype('<i2').tobytes()
    s = _to_array(pcm)
    ratio = src / dst
    n = int(len(s) / ratio)
    out = array('h', bytes(2 * n))
    last = len(s) - 1
    for i in range(n):
        pos = i * ratio
        j = int(pos)
        a = s[j]
        b = s[j + 1] if j < last else a
        out[i] = int(a + (b - a) * (pos - j))
    return _to_bytes(out)


def to_16k_mono(pcm, rate, channels=1):
    """Mix interleaved s16le PCM down to mono and resample to 16 kHz."""
    if channels > 1:
        s = _to_array(pcm)
        usable = len(s) // channels * channels
        mono = array('h', (sum(s[i:i + channels]) // channels for i in range(0, usable, channels)))
        pcm = _to_bytes(mono)
    return resample(pcm, rate, RATE)


def parse_audio(data):
    """Accept a WAV file or raw 16 kHz mono s16le PCM from the device; return 16 kHz mono s16le PCM."""
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        with wave.open(io.BytesIO(data)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError('WAV audio must be 16-bit')
        return to_16k_mono(frames, rate, channels)
    return data[:len(data) // 2 * 2]


def wav_bytes(pcm, rate=RATE):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def rms(pcm):
    """Root-mean-square level of s16le PCM (0 for silence, about 32768 for full scale)."""
    s = _to_array(pcm)
    if not s:
        return 0.0
    return (sum(v * v for v in s) / len(s)) ** 0.5


def seconds(pcm, rate=RATE):
    return len(pcm) / 2 / rate

# ------------------------------------------------------------------ language

_NL = set('de het een ik je jij u niet dat van voor met te zijn maar ook nog wat hoe kan wil heb hebt dit er naar om bij als dan wel geen mijn jouw vandaag morgen goedemorgen herinner herinnering wekker alsjeblieft bedankt hallo hoi gaat goed prima'.split())
_EN = set('the a an and are to of in for you your i it that this with on be not have will can what how today tomorrow reminder alarm please thanks hello hi good morning'.split())


def guess_lang(text, default='en'):
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    nl = sum(1 for w in words if w in _NL)
    en = sum(1 for w in words if w in _EN)
    if nl > en:
        return 'nl'
    if en > nl:
        return 'en'
    return default


_JUNK = re.compile(r"thanks? for watching|thank you for watching|subtitles? by|ondertitel|amara\.org|bedankt voor het kijken|untertitel|^\W*(?:you|bye|uh|um|hmm+)\W*$", re.I)


def looks_like_noise(text):
    """Whisper invents captions for silence or noise; treat those as 'nothing heard'."""
    t = (text or '').strip()
    return len(re.sub(r'\W', '', t)) < 2 or bool(_JUNK.search(t))

# ------------------------------------------------------------------ text for speech

_CODE_NOTE = {'en': 'I put the code in the chat.', 'nl': 'De code staat in de chat.'}
_MORE_NOTE = {'en': 'The rest is in the chat.', 'nl': 'De rest staat in de chat.'}


def speech_text(text, lang='en', max_chars=700):
    """Turn a chat answer (Markdown, lists, sources, code) into plain sentences that sound right when spoken."""
    t = str(text or '')
    t = re.sub(r'<think>.*?</think>', ' ', t, flags=re.S | re.I)
    t = re.sub(r'<think>.*', ' ', t, flags=re.S | re.I)
    had_code = bool(re.search(r'```', t))
    t = re.sub(r'```.*?```', ' ', t, flags=re.S)
    t = re.split(r'\n\s*(?:sources?|bronnen)\s*:\s*\n', t, maxsplit=1, flags=re.I)[0]
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)
    t = re.sub(r'https?://\S+', ' ', t)
    t = t.replace('`', '')
    t = re.sub(r'(\*\*|__)(.*?)\1', r'\2', t)
    t = re.sub(r'(?<![\w*])\*(?!\s)(.*?)(?<!\s)\*(?![\w*])', r'\1', t)
    t = re.sub(r'^\s{0,3}#{1,6}\s*', '', t, flags=re.M)
    t = re.sub(r'^\s*\|?[-:| ]{3,}\|?\s*$', '', t, flags=re.M)
    t = t.replace('|', ', ')
    t = re.sub(r'\b0(\d):(\d\d)\b', r'\1:\2', t)
    lines = []
    for raw in t.splitlines():
        line = re.sub(r'^\s*(?:[-*\u2022]|\d+[.)])\s+', '', raw).strip()
        if not line:
            continue
        if line[-1] not in '.!?:;,\u2026':
            line += '.'
        lines.append(line.rstrip(':'))
    t = ' '.join(lines)
    t = re.sub(r'\s+', ' ', t).strip()
    if had_code:
        t = (t + ' ' + _CODE_NOTE.get(lang, _CODE_NOTE['en'])).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
        t = (cut[:end + 1] if end > max_chars // 3 else cut.rsplit(' ', 1)[0] + '.') + ' ' + _MORE_NOTE.get(lang, _MORE_NOTE['en'])
    return t


def split_sentences(text, limit=220):
    """Split into chunks that each fit one text-to-speech call, so audio can start before the whole reply is synthesised."""
    parts = re.split(r'(?<=[.!?\u2026])\s+', text.strip())
    chunks = []
    for part in parts:
        part = part.strip()
        while len(part) > limit:
            cut = max(part.rfind(', ', 0, limit), part.rfind('; ', 0, limit))
            if cut < limit // 3:
                cut = part.rfind(' ', 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            chunks.append(part)
    return chunks

# ------------------------------------------------------------------ spoken commands handled without the model

_INTENTS = (
    ('sleep', re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:(?:sage|jarvis)\W+)?(?:good\s?night|go\s+to\s+sleep|sleep\s+mode|goede\s?nacht|welterusten|ga\s+slapen|slaapmodus)\b", re.I)),
    ('wake', re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:(?:sage|jarvis)\W+)?(?:good\s+morning|wake\s+up|i(?:'m|\s+am)\s+up|goedemorgen|goeiemorgen|ik\s+ben\s+wakker|wakker\s+worden)\b", re.I)),
    ('audit', re.compile(r"\b(?:(?:voice|speaker)\s+(?:log|audit|activity|history)|who\s+(?:has\s+)?(?:talked|spoken|used)|what\s+have\s+you\s+heard|wie\s+heeft\s+(?:er\s+)?gesproken|spraaklog)\b", re.I)),
    ('backup', re.compile(r"\b(?:how|when|did|is|was|status|hoe|wanneer)\b.*\bback-?ups?\b|\bback-?up\s+status\b", re.I)),
    ('recap', re.compile(r"\b(?:what\s+(?:did\s+you|have\s+you)\s+(?:do|done|been\s+doing)|what\s+happened|while\s+i\s+was\s+(?:gone|away|out|asleep)|anything\s+happen(?:ed)?|wat\s+heb\s+je\s+gedaan|wat\s+is\s+er\s+gebeurd|terwijl\s+ik\s+weg\s+was)\b", re.I)),
    ('timers', re.compile(r"\b(?:(?:what|which|any|how\s+many|list|do\s+i\s+have)\b.*\b(?:timers?|reminders?|alarms?)|how\s+(?:long|much\s+time)\s+(?:is\s+)?left|welke\s+(?:timers?|herinneringen|wekkers?)|hoe\s+lang\s+nog)\b", re.I)),
    ('needs', re.compile(r"\b(?:needs?\s+(?:my\s+)?(?:attention|input)|waiting\s+for\s+me|anything\s+for\s+me|status\s+(?:report|update)|give\s+me\s+(?:a\s+)?status|wat\s+heeft\s+.{0,20}aandacht|is\s+er\s+iets\s+voor\s+mij)\b", re.I)),
)


def match_intent(text):
    """Name of the spoken command in `text` (sleep, wake, audit, backup, recap, timers, needs) or ''.
    Only short utterances count, so a long sentence that merely mentions 'good night' goes to the model."""
    t = (text or '').strip()
    words = len(t.split())
    if not t or words > 14:
        return ''
    for name, pattern in _INTENTS:
        if pattern.search(t) and (name != 'timers' or words <= 10):
            return name
    return ''

# ------------------------------------------------------------------ request signing

def sign(secret, timestamp, method, path, body=b''):
    """HMAC-SHA256 over timestamp, method, path (with query) and the SHA-256 of the body. The device sends this
    as X-Sage-Sig together with X-Sage-Time, so a captured request cannot be replayed or edited."""
    import hashlib
    import hmac
    message = f'{timestamp}.{method}.{path}.{hashlib.sha256(body).hexdigest()}'
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
