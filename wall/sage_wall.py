#!/usr/bin/env python3
"""Sage wall display: a small native touch client for a Raspberry Pi (built for a Pi Zero W + 7" screen).

No browser needed (Chromium does not run properly on the original Pi Zero's ARMv6 CPU). The client polls
the Sage server over Wi-Fi (GET /api/wall) and draws the clock, calendar, what Sage is working on and
anything that needs you. By touch you can approve or deny tool requests and chat with Sage (streamed
replies, on-screen keyboard).

Run:   python3 sage_wall.py                  (full screen; use the systemd service for boot)
Test:  python3 sage_wall.py --demo --windowed 1024x600     (fake data, no server needed)
"""
import argparse
import datetime as dt
import glob
import http.client
import json
import math
import os
import queue
import random
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

DEFAULTS = {
    'SAGE_URL': 'http://192.168.1.10:8001',   # the PC that runs Sage
    'POLL_SECONDS': '15',
    'DIM_AFTER_SECONDS': '180',               # dim the screen after this much inactivity
    'NIGHT_START': '23:00',                   # clock-only, very dark, between these times
    'NIGHT_END': '07:00',
    'NIGHT_WAKE_SECONDS': '45',               # how long a touch wakes the screen at night
    'FONT': '',                               # optional path to a .ttf font
    'USER_NAME': '',                          # shown in the greeting, e.g. Kyan
}
CONFIG_PATHS = ['/etc/sage-wall.conf', os.path.expanduser('~/.config/sage-wall.conf'), 'sage-wall.conf']

# ---- palette
BG_TOP, BG_BOT = (10, 16, 30), (7, 10, 18)
CARD, CARD_LINE = (18, 28, 47), (36, 52, 82)
TEXT, MUTED, DIM = (236, 242, 252), (140, 157, 186), (86, 102, 132)
ACCENT, TEAL = (84, 146, 255), (52, 208, 186)
AMBER, RED, GREEN = (255, 188, 76), (244, 104, 104), (74, 206, 130)


def load_config(path=None):
    cfg = dict(DEFAULTS)
    for candidate in ([path] if path else CONFIG_PATHS):
        try:
            with open(candidate, encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    cfg[key.strip()] = value.strip().strip('"\'')
        except OSError:
            pass
    for key in DEFAULTS:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


# ---------------------------------------------------------------- network

class ApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Api:
    """Tiny HTTP client using only the standard library (keeps RAM use low on a Pi Zero)."""

    def __init__(self, base):
        parsed = urlparse(base if '//' in base else 'http://' + base)
        self.https = parsed.scheme == 'https'
        self.host = parsed.hostname or '127.0.0.1'
        self.port = parsed.port or (443 if self.https else 80)
        self.base = f'{parsed.scheme}://{parsed.netloc}'

    def _conn(self, timeout):
        factory = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return factory(self.host, self.port, timeout=timeout)

    def _request(self, method, path, body=None, timeout=8):
        conn = self._conn(timeout)
        try:
            headers = {'Content-Type': 'application/json'} if body is not None else {}
            conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
            response = conn.getresponse()
            data = response.read()
            try:
                parsed = json.loads(data or b'{}')
            except ValueError:
                parsed = {}
            if response.status >= 400:
                raise ApiError(parsed.get('error') or f'HTTP {response.status}', response.status)
            return parsed
        except (OSError, http.client.HTTPException) as exc:
            raise ApiError(str(exc) or exc.__class__.__name__) from exc
        finally:
            conn.close()

    def get(self, path, timeout=8):
        return self._request('GET', path, None, timeout)

    def post(self, path, body, timeout=180):
        return self._request('POST', path, body, timeout)

    def cancel(self, sid):
        try:
            self._request('POST', '/api/chat/cancel', {'id': sid}, 5)
        except ApiError:
            pass

    def stream_chat(self, text, sid, emit, stop_flag):
        """POST /api/chat/stream and call emit(event) for every newline-delimited JSON event."""
        conn = self._conn(120)
        stopped_at = None
        try:
            conn.request('POST', '/api/chat/stream', body=json.dumps({'id': sid, 'message': text}).encode(),
                         headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status != 200:
                data = response.read()
                try:
                    message = json.loads(data).get('error')
                except ValueError:
                    message = None
                raise ApiError(message or f'HTTP {response.status}', response.status)
            for raw in response:
                line = raw.strip()
                if line:
                    emit(json.loads(line))
                if stop_flag.is_set():
                    stopped_at = stopped_at or time.time()
                    if time.time() - stopped_at > 5:
                        break
        except (OSError, http.client.HTTPException) as exc:
            raise ApiError(str(exc) or exc.__class__.__name__) from exc
        finally:
            conn.close()


def demo_wall():
    now = dt.datetime.now().astimezone()

    def at(hours, minutes=0):
        return (now + dt.timedelta(hours=hours)).replace(minute=minutes, second=0, microsecond=0).isoformat(timespec='minutes')
    return {
        'model': 'qwen2.5-coder:7b', 'ollama_online': True, 'queued': 3,
        'approvals': [{'id': 7, 'tool': 'launch_app', 'summary': 'app: spotify', 'created_at': ''}],
        'events': [{'subject': 'Thermodynamics lecture', 'start': at(1, 0), 'end': at(2, 45), 'location': 'Auditorium', 'is_all_day': False},
                   {'subject': 'ROV: test RS485 link', 'start': at(4, 30), 'end': at(5, 30), 'location': '', 'is_all_day': False},
                   {'subject': 'Dinner with Elles', 'start': at(8, 0), 'end': at(9, 0), 'location': '', 'is_all_day': False},
                   {'subject': 'Gym', 'start': at(26, 0), 'end': at(27, 0), 'location': '', 'is_all_day': False}],
        'overdue': [{'title': 'Order depth sensor', 'due_date': '2026-10-08', 'project': 'ROV'}],
        'next_tasks': [{'title': 'Define the export plugin inputs', 'priority': 'high', 'project': 'Export Plugin'},
                       {'title': 'Wire the second Arduino Nano', 'priority': 'medium', 'project': 'ROV'},
                       {'title': 'Draft the PHYSIX launch post', 'priority': 'low', 'project': 'SimVerse'}],
        'working': [{'title': 'Project task: write RS485 framing notes', 'status': 'running', 'checkpoint': 'Drafting section 2 of 4'}],
        'recent_jobs': [{'title': 'Session summary', 'updated_at': ''}],
        'last_message': {'role': 'assistant', 'content': 'Your next best action is to finish the RS485 framing notes, then order the depth sensor. The Export Plugin project has been created.', 'created_at': ''},
        'focus': None,
        'timers': [{'id': 3, 'kind': 'timer', 'title': 'Pasta', 'seconds_left': 272, 'when': 'in 5 min'},
                   {'id': 4, 'kind': 'reminder', 'title': 'Call the dentist', 'seconds_left': 86400, 'when': 'tomorrow at 10:30'}],
        'alerts': [], 'events_cursor': 0,
        'needs_input': [{'id': 12, 'title': 'Choose the depth sensor', 'project': 'ROV', 'kind': 'decision',
                         'question': 'Which depth sensor should I plan the wiring for: the MS5837-30BA (waterproof, rated to 30 bar) or the cheaper BMP388 (not waterproof)? I need your choice before I can write the wiring notes and the driver.'}],
    }


class DemoApi:
    base = 'demo'

    def get(self, path, timeout=8):
        if path.startswith('/api/events'):
            time.sleep(20)
            return {'events': [], 'cursor': 0}
        if path.startswith('/api/voice/state'):
            time.sleep(20)
            return {'version': 1, 'ring': 'idle', 'asleep': False, 'device_online': True}
        return demo_wall()

    def post(self, path, body, timeout=180):
        time.sleep(0.5)
        return {'answer': 'Approved. I opened the app.'}

    def cancel(self, sid):
        pass

    def stream_chat(self, text, sid, emit, stop_flag):
        emit({'type': 'start', 'id': sid})
        emit({'type': 'status', 'text': 'thinking\u2026'})
        time.sleep(0.5)
        reply = ('Here is a quick demo reply. On the real display this text streams in word by word from your '
                 'Sage server, and Stop cancels it immediately.')
        for word in reply.split(' '):
            if stop_flag.is_set():
                break
            emit({'type': 'token', 'text': word + ' '})
            time.sleep(0.07)
        emit({'type': 'done', 'answer': reply, 'stopped': stop_flag.is_set()})


class Poller(threading.Thread):
    def __init__(self, api, interval, out):
        super().__init__(daemon=True)
        self.api, self.interval, self.out = api, max(3, interval), out
        self.kick = threading.Event()
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                self.out.put(('wall', self.api.get('/api/wall')))
            except ApiError as exc:
                self.out.put(('offline', str(exc), exc.status))
            self.kick.wait(self.interval)
            self.kick.clear()


class EventListener(threading.Thread):
    """Long-polls /api/events so the screen reacts the moment something happens (timer rings, task finishes ...)."""

    def __init__(self, api, out):
        super().__init__(daemon=True)
        self.api, self.out = api, out
        self.halt = threading.Event()

    def run(self):
        cursor = None
        while not self.halt.is_set():
            try:
                if cursor is None:
                    cursor = self.api.get('/api/events').get('cursor', 0)
                data = self.api.get(f'/api/events?since={cursor}&wait=25', timeout=40)
                for event in data.get('events', []):
                    self.out.put(('event', event))
                cursor = data.get('cursor', cursor)
            except ApiError:
                self.halt.wait(5)


class VoiceListener(threading.Thread):
    """Long-polls /api/voice/state so the light ring and sleep mode of the speaker follow it instantly."""

    def __init__(self, api, out):
        super().__init__(daemon=True)
        self.api, self.out = api, out
        self.halt = threading.Event()

    def run(self):
        version = -1
        while not self.halt.is_set():
            try:
                data = self.api.get(f'/api/voice/state?since={version}&wait=25', timeout=40)
                version = data.get('version', version)
                self.out.put(('voice', data))
            except ApiError as exc:
                self.halt.wait(60 if exc.status in (403, 404) else 5)     # older Sage without voice: ask rarely

# ---------------------------------------------------------------- fonts + text helpers

FONT_FAMILIES = [  # (light, regular, medium, bold)
    ('Roboto-Light', 'Roboto-Regular', 'Roboto-Medium', 'Roboto-Bold'),
    ('Inter-Light', 'Inter-Regular', 'Inter-Medium', 'Inter-Bold'),
    ('NotoSans-Light', 'NotoSans-Regular', 'NotoSans-Medium', 'NotoSans-Bold'),
    ('OpenSans-Light', 'OpenSans-Regular', 'OpenSans-Medium', 'OpenSans-Bold'),
    (None, 'DejaVuSans', None, 'DejaVuSans-Bold'),
    (None, 'LiberationSans-Regular', None, 'LiberationSans-Bold'),
]


def find_fonts(preferred=''):
    """Return {'light','regular','medium','bold','fallback'} -> font file paths (None when not found)."""
    index = {}
    roots = ['/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts'),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')]
    for root in roots:
        for path in glob.glob(os.path.join(root, '**', '*.[to]tf'), recursive=True):
            index.setdefault(os.path.splitext(os.path.basename(path))[0], path)
    found = {'light': None, 'regular': None, 'medium': None, 'bold': None}
    if preferred and os.path.isfile(preferred):
        found['regular'] = preferred
    else:
        for light, regular, medium, bold in FONT_FAMILIES:
            if regular in index:
                found = {'light': index.get(light), 'regular': index[regular], 'medium': index.get(medium), 'bold': index.get(bold)}
                break
    found['fallback'] = index.get('DejaVuSans')
    return found


def plain(text):
    """Strip the Markdown noise that looks bad in a plain text renderer."""
    out = []
    for line in str(text or '').replace('\r', '').split('\n'):
        stripped = line.strip()
        if stripped.startswith('```'):
            continue
        stripped = line.replace('**', '').replace('__', '').replace('`', '').replace('*(stopped)*', '(stopped)')
        if stripped.lstrip().startswith('#'):
            stripped = stripped.lstrip('# ').strip()
        if stripped.lstrip().startswith(('* ', '- ')):
            stripped = '\u2022 ' + stripped.lstrip()[2:]
        out.append(stripped)
    return '\n'.join(out).strip()


def parse_hhmm(value, default):
    try:
        hours, minutes = str(value).split(':')
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return default


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---------------------------------------------------------------- painter (cached, anti-aliased shapes)

class Painter:
    """Draws text and shapes. Every shape is rendered once (supersampled for smooth edges) and cached."""

    def __init__(self, pg, screen, S, fonts):
        self.pg, self.screen, self.S, self.paths = pg, screen, S, fonts
        self.fonts, self.texts, self.shapes = {}, {}, {}

    # -- text
    def font(self, size, weight='regular', fallback=False):
        key = (int(size), weight, fallback)
        font = self.fonts.get(key)
        if font is None:
            path = self.paths.get('fallback') if fallback else (self.paths.get(weight) or self.paths.get('regular'))
            try:
                font = self.pg.font.Font(path, int(size)) if path else self.pg.font.Font(None, int(size * 1.25))
            except Exception:
                font = self.pg.font.Font(None, int(size * 1.25))
            self.fonts[key] = font
        return font

    def text_font(self, s, size, weight):
        font = self.font(size, weight)
        if not s.isascii() and self.paths.get('fallback'):
            try:
                if any(m is None for m in font.metrics(s)):
                    return self.font(size, weight, True)
            except Exception:
                pass
        return font

    def text_surface(self, s, size, color, weight='regular'):
        key = (s, int(size), color, weight)
        surf = self.texts.get(key)
        if surf is None:
            if len(self.texts) > 700:
                self.texts.clear()
            surf = self.text_font(s, size, weight).render(s, True, color)
            self.texts[key] = surf
        return surf

    def width(self, s, size, weight='regular'):
        return self.text_font(s, size, weight).size(s)[0]

    def fit(self, s, size, max_w, weight='regular'):
        width = self.width(s, size, weight)
        if width <= max_w:
            return s
        s = s[:max(1, int(len(s) * max_w / max(width, 1)))]
        while len(s) > 1 and self.width(s + '\u2026', size, weight) > max_w:
            s = s[:-1]
        return s.rstrip() + '\u2026'

    def wrap(self, s, size, max_w, weight='regular', max_lines=None):
        lines = []
        for para in str(s).split('\n'):
            current = ''
            for word in para.split(' '):
                trial = (current + ' ' + word) if current else word
                if self.width(trial, size, weight) <= max_w:
                    current = trial
                    continue
                if current:
                    lines.append(current)
                while len(word) > 1 and self.width(word, size, weight) > max_w:
                    cut = len(word)
                    while cut > 1 and self.width(word[:cut], size, weight) > max_w:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                current = word
            lines.append(current)
        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + ' \u2026', size, max_w, weight)
        return lines

    def text(self, x, y, s, size, color=TEXT, weight='regular', anchor='tl', max_w=None):
        if max_w:
            s = self.fit(s, size, max_w, weight)
        surf = self.text_surface(s, size, color, weight)
        rect = surf.get_rect()
        setattr(rect, {'tl': 'topleft', 'tr': 'topright', 'tc': 'midtop', 'mc': 'center', 'ml': 'midleft', 'mr': 'midright'}[anchor], (x, y))
        self.screen.blit(surf, rect)
        return rect

    # -- shapes (cached)
    def _cached(self, key, build):
        surf = self.shapes.get(key)
        if surf is None:
            if len(self.shapes) > 260:
                self.shapes.clear()
            surf = self.shapes[key] = build()
        return surf

    def rrect_surface(self, w, h, r, fill, border=None, bw=2, k=3):
        pg = self.pg

        def build():
            big = pg.Surface((w * k, h * k), pg.SRCALPHA)
            if border:
                pg.draw.rect(big, border, (0, 0, w * k, h * k), border_radius=r * k)
                inner = pg.Rect(bw * k, bw * k, (w - 2 * bw) * k, (h - 2 * bw) * k)
                pg.draw.rect(big, fill, inner, border_radius=max(0, (r - bw) * k))
            else:
                pg.draw.rect(big, fill, (0, 0, w * k, h * k), border_radius=r * k)
            return pg.transform.smoothscale(big, (w, h))
        return self._cached(('rr', w, h, r, fill, border, bw), build)

    def shadow_surface(self, w, h, r, spread, alpha):
        pg = self.pg
        q = 6

        def build():
            small = pg.Surface((max(2, (w + 2 * spread) // q), max(2, (h + 2 * spread) // q)), pg.SRCALPHA)
            pg.draw.rect(small, (0, 0, 0, alpha), (spread // q, spread // q, max(1, w // q), max(1, h // q)), border_radius=max(1, r // q))
            return pg.transform.smoothscale(small, (w + 2 * spread, h + 2 * spread))
        return self._cached(('sh', w, h, r, spread, alpha), build)

    def gradient_surface(self, w, h, top, bottom, left=None, right=None):
        pg = self.pg

        def build():
            tiny = pg.Surface((2, 2))
            tl, tr = (left or top), (right or top)
            bl, br = (left or bottom), (right or bottom)
            tiny.set_at((0, 0), tl)
            tiny.set_at((1, 0), tr)
            tiny.set_at((0, 1), bl)
            tiny.set_at((1, 1), br)
            return pg.transform.smoothscale(tiny, (w, h))
        return self._cached(('gr', w, h, top, bottom, left, right), build)

    def gradient_rrect(self, w, h, r, c1, c2):
        pg = self.pg

        def build():
            base = self.gradient_surface(w, h, c1, c2, c1, c2).convert_alpha()   # diagonal-ish blend
            base.blit(self.rrect_surface(w, h, r, (255, 255, 255, 255)), (0, 0), special_flags=pg.BLEND_RGBA_MULT)
            return base
        return self._cached(('gradrr', w, h, r, c1, c2), build)

    def glow_surface(self, radius, color, alpha, steps=56):
        pg = self.pg

        def build():
            surf = pg.Surface((radius * 2, radius * 2), pg.SRCALPHA)
            for i in range(steps):
                frac = i / steps
                pg.draw.circle(surf, (*color, int(alpha * frac ** 2.2)), (radius, radius), int(radius * (1 - frac)))
            return surf
        return self._cached(('gl', radius, color, alpha), build)

    def glow(self, cx, cy, radius, color, alpha):
        self.screen.blit(self.glow_surface(radius, color, alpha), (cx - radius, cy - radius))

    def card(self, rect, fill=CARD, border=CARD_LINE, radius=18, shadow=True, glow=None):
        S = self.S
        rect = self.pg.Rect(rect)
        if glow:
            self.glow(rect.centerx, rect.centery, int(max(rect.w, rect.h) * 0.62), glow, 55)
        if shadow:
            spread = S(22)
            self.screen.blit(self.shadow_surface(rect.w, rect.h, S(radius), spread, 120), (rect.x - spread, rect.y - spread + S(8)))
        self.screen.blit(self.rrect_surface(rect.w, rect.h, S(radius), fill, border, max(1, S(1))), rect.topleft)

    def pill(self, rect, fill, border=None):
        rect = self.pg.Rect(rect)
        self.screen.blit(self.rrect_surface(rect.w, rect.h, rect.h // 2, fill, border, 1), rect.topleft)

    def icon_surface(self, name, size, color):
        pg = self.pg
        k = 4

        def build():
            big = pg.Surface((size * k, size * k), pg.SRCALPHA)
            u = size * k / 24.0
            P = lambda x, y: (int(x * u), int(y * u))
            w = max(2, int(2.2 * u))
            if name == 'calendar':
                pg.draw.rect(big, color, (int(3 * u), int(5 * u), int(18 * u), int(16 * u)), width=w, border_radius=int(3 * u))
                pg.draw.line(big, color, P(3, 10), P(21, 10), w)
                pg.draw.line(big, color, P(8, 3), P(8, 7), w)
                pg.draw.line(big, color, P(16, 3), P(16, 7), w)
            elif name == 'bell':
                pg.draw.circle(big, color, P(12, 10), int(6.5 * u))
                pg.draw.rect(big, color, (int(5.5 * u), int(10 * u), int(13 * u), int(6 * u)))
                pg.draw.polygon(big, color, [P(4, 17.5), P(20, 17.5), P(18, 15), P(6, 15)])
                pg.draw.circle(big, color, P(12, 20), int(2.2 * u))
                pg.draw.line(big, color, P(12, 2.5), P(12, 4.5), w)
            elif name == 'check':
                pg.draw.lines(big, color, False, [P(4.5, 12.5), P(10, 18), P(19.5, 6.5)], int(3 * u))
            elif name == 'checkbox':
                pg.draw.rect(big, color, (int(3.5 * u), int(3.5 * u), int(17 * u), int(17 * u)), width=w, border_radius=int(4 * u))
            elif name == 'chat':
                pg.draw.rect(big, color, (int(3 * u), int(4 * u), int(18 * u), int(13 * u)), width=w, border_radius=int(4 * u))
                pg.draw.polygon(big, color, [P(8, 16.5), P(8, 22), P(13.5, 16.5)])
            elif name == 'moon':
                pg.draw.circle(big, color, P(12, 12), int(9 * u))
                pg.draw.circle(big, (0, 0, 0, 0), P(17, 8), int(8 * u))
            elif name == 'chevron':
                pg.draw.lines(big, color, False, [P(15, 4), P(7, 12), P(15, 20)], int(3 * u))
            elif name == 'spark':
                pg.draw.polygon(big, color, [P(12, 1.5), P(14.4, 9.6), P(22.5, 12), P(14.4, 14.4), P(12, 22.5), P(9.6, 14.4), P(1.5, 12), P(9.6, 9.6)])
            elif name == 'shift':
                pg.draw.polygon(big, color, [P(12, 3), P(21.5, 13), P(15.5, 13), P(15.5, 21), P(8.5, 21), P(8.5, 13), P(2.5, 13)])
            elif name == 'stop':
                pg.draw.rect(big, color, (int(6 * u), int(6 * u), int(12 * u), int(12 * u)), border_radius=int(2.5 * u))
            elif name == 'send':
                pg.draw.polygon(big, color, [P(3, 3.5), P(22, 12), P(3, 20.5), P(6.5, 12)])
            return pg.transform.smoothscale(big, (size, size))
        return self._cached(('ic', name, size, color), build)

    def icon(self, name, cx, cy, size, color=TEXT):
        surf = self.icon_surface(name, size, color)
        self.screen.blit(surf, (cx - size // 2, cy - size // 2))

    def orb(self, cx, cy, radius, t, color=ACCENT, glow=True):
        """Sage's mark: a softly pulsing orb with one small satellite."""
        pg = self.pg
        pulse = 0.5 + 0.5 * math.sin(t * 2.4)
        if glow:
            self.glow(cx, cy, int(radius * 2.6), color, int(70 + 40 * pulse))
        layers = ((1.0, (16, 30, 58)), (0.8, (24, 50, 100)), (0.6, mix(color, (20, 40, 90), 0.45)), (0.4, color))
        for index, (scale, shade) in enumerate(layers):
            grow = 1 + 0.07 * pulse * (len(layers) - index) / len(layers)
            pg.draw.circle(self.screen, shade, (cx, cy), max(2, int(radius * scale * grow)))
        pg.draw.circle(self.screen, (238, 246, 255), (cx, cy), max(1, int(radius * 0.13)))
        a = t * 1.5
        pg.draw.circle(self.screen, TEAL, (int(cx + math.cos(a) * radius * 1.2), int(cy + math.sin(a) * radius * 1.2)), max(2, int(radius * 0.075)))


def relative_time(start, end, now):
    now = now.astimezone()
    if start <= now <= end:
        return 'now'
    minutes = int((start - now).total_seconds() // 60)
    if minutes < 1:
        return 'starting'
    if minutes < 60:
        return f'in {minutes} min'
    if minutes < 24 * 60:
        return f'in {minutes // 60} h {minutes % 60:02d} min'
    return start.strftime('%A')


class WallApp:
    def __init__(self, cfg, api, screen, pygame_module):
        self.pg = pygame_module
        self.cfg, self.api, self.screen = cfg, api, screen
        self.W, self.H = screen.get_size()
        self.S = lambda n: max(1, int(n * self.H / 600))
        self.p = Painter(pygame_module, screen, self.S, find_fonts(cfg.get('FONT', '')))
        self.bg = None
        self.dim_overlay = None
        self.q = queue.Queue()
        self.wall, self.online, self.error, self.error_status = None, False, '', 0
        self.page = 'home'
        self.chat, self.sid, self.streaming = [], None, False
        self.stop_flag = threading.Event()
        self.typing, self.input, self.shift, self.symbols = False, '', False, False
        self.scroll = 0
        self.toast, self.toast_until = '', 0.0
        self.last_input = time.time()
        self.sleeping = False
        self.buttons, self.pressed, self.down, self.dragged = [], None, None, False
        self.busy_approvals = set()
        self.prev_approvals = 0
        self.mode, self.minute_key, self.running, self.dirty = 'awake', None, True, True
        self.night_start = parse_hhmm(cfg['NIGHT_START'], 23 * 60)
        self.night_end = parse_hhmm(cfg['NIGHT_END'], 7 * 60)
        self.dim_after = float(cfg['DIM_AFTER_SECONDS'])
        self.night_wake = float(cfg['NIGHT_WAKE_SECONDS'])
        self.poller = Poller(api, float(cfg['POLL_SECONDS']), self.q)
        self.listener = EventListener(api, self.q)
        self.voice, self.voice_listener = None, VoiceListener(api, self.q)
        self.ringing, self.wall_at, self._sec = [], 0.0, 0
        self.task = None
        self.brief = None            # the wake-up brief from your phone, shown until you tap the screen
        self.boot_t0, self.booted = time.time(), False
        rng = random.Random(7)
        self.stars = [(rng.random(), rng.random(), rng.random() * 6.28, 0.6 + rng.random() * 1.4) for _ in range(46)]
        self.ring_layer = None

    # ------------------------------------------------------------ small helpers
    def text(self, *args, **kwargs):
        return self.p.text(*args, **kwargs)

    def background(self):
        if self.bg is None:
            p, W, H, S = self.p, self.W, self.H, self.S
            bg = p.gradient_surface(W, H, BG_TOP, BG_BOT).copy()
            for cx, cy, radius, color, alpha in ((S(250), S(150), S(430), ACCENT, 46), (W - S(120), H - S(60), S(380), TEAL, 26)):
                bg.blit(p.glow_surface(radius, color, alpha), (cx - radius, cy - radius))
            self.bg = bg
        self.screen.blit(self.bg, (0, 0))

    def button(self, rect, label, action, kind='secondary', enabled=True, size=22, icon=None):
        p, S = self.p, self.S
        rect = self.pg.Rect(rect)
        self.buttons.append((rect, action if enabled else None))
        pressed = self.pressed == rect.topleft + rect.size
        radius = min(S(16), rect.h // 2)
        shade = (lambda c: tuple(min(255, int(v * 1.25) + 10) for v in c)) if pressed else (lambda c: c)
        if kind == 'primary':
            surf = p.gradient_rrect(rect.w, rect.h, radius, shade((58, 112, 236)), shade((46, 188, 190)))
            self.screen.blit(p.shadow_surface(rect.w, rect.h, radius, S(16), 90), (rect.x - S(16), rect.y - S(16) + S(6)))
            self.screen.blit(surf, rect.topleft)
        else:
            fills = {'secondary': ((26, 38, 62), (50, 68, 104)), 'ghost': ((16, 24, 40), (34, 48, 76)), 'key': ((30, 44, 70), (54, 74, 112)),
                     'special': ((20, 30, 50), (40, 56, 88)), 'danger': ((120, 44, 52), (190, 76, 84)), 'success': ((30, 118, 82), (60, 180, 130))}
            fill, border = fills[kind]
            if not enabled:
                fill, border = tuple(int(v * 0.65) for v in fill), tuple(int(v * 0.65) for v in border)
            self.screen.blit(p.rrect_surface(rect.w, rect.h, radius, shade(fill), border, 1), rect.topleft)
        color = TEXT if enabled else DIM
        weight = 'bold' if kind in ('primary', 'danger', 'success') else 'medium'
        if icon:
            label_w = p.width(label, self.S(size), weight) if label else 0
            gap = S(10) if label else 0
            isz = S(size + 4)
            x0 = rect.centerx - (isz + gap + label_w) // 2
            p.icon(icon, x0 + isz // 2, rect.centery, isz, color)
            if label:
                self.text(x0 + isz + gap, rect.centery, label, S(size), color, weight, 'ml')
        else:
            self.text(rect.centerx, rect.centery, label, S(size), color, weight, 'mc', max_w=rect.w - S(10))
        return rect

    def hit(self, pos):
        for rect, action in reversed(self.buttons):
            if rect.collidepoint(pos):
                return rect, action
        return None, None

    # ------------------------------------------------------------ state
    def touch(self):
        self.last_input = time.time()
        self.sleeping = False
        self.dirty = True

    def in_night(self, now):
        minutes = now.hour * 60 + now.minute
        if self.night_start <= self.night_end:
            return self.night_start <= minutes < self.night_end
        return minutes >= self.night_start or minutes < self.night_end

    def compute_mode(self, now):
        idle = time.time() - self.last_input
        important = bool(self.wall and self.wall.get('approvals')) or self.streaming or bool(self.ringing) or bool(self.brief)
        if important:
            return 'awake'
        if self.sleeping:
            return 'clock'
        if self.in_night(now) and idle > self.night_wake:
            return 'clock'
        if idle > self.dim_after:
            return 'dim'
        return 'awake'

    def say(self, message, seconds=3.5):
        self.toast, self.toast_until, self.dirty = message, time.time() + seconds, True

    def upcoming_events(self, now):
        found = []
        for event in (self.wall or {}).get('events', []):
            try:
                start = dt.datetime.fromisoformat(event['start']).astimezone()
                end = dt.datetime.fromisoformat(event['end']).astimezone()
            except (KeyError, ValueError, TypeError):
                continue
            if end < now.astimezone():
                continue
            found.append((start, end, event))
        found.sort(key=lambda item: item[0])
        return found

    # ------------------------------------------------------------ actions
    def open_page(self, page):
        self.page, self.dirty = page, True
        if page == 'chat':
            self.scroll = 0

    def open_task(self, item):
        self.task, self.page, self.typing, self.input, self.dirty = item, 'task', False, '', True

    def close_task(self):
        self.task, self.page, self.typing, self.input, self.dirty = None, 'home', False, '', True

    def submit_input(self):
        if self.page == 'task':
            self.send_task_input(self.input, self.task.get('kind') == 'physical')
        else:
            self.start_chat(self.input)

    def send_task_input(self, text, done=False):
        task = self.task
        if not task or (not text.strip() and not done):
            return
        self.say('Sending\u2026')

        def work():
            try:
                result = self.api.post(f"/api/plan/task/{task['id']}/input", {'text': text.strip(), 'mark_done': done}, 30)
                self.q.put(('task_sent', task['id'], str(result.get('status', ''))))
            except ApiError as exc:
                self.q.put(('approval_error', -1, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def start_chat(self, message):
        message = message.strip()
        if not message or self.streaming:
            return
        self.page, self.typing, self.input, self.scroll = 'chat', False, '', 0
        self.chat.append({'role': 'user', 'text': message})
        reply = {'role': 'assistant', 'text': '', 'status': 'thinking\u2026'}
        self.chat.append(reply)
        del self.chat[:-20]
        self.sid = uuid.uuid4().hex
        self.streaming = True
        self.stop_flag = threading.Event()
        sid, flag = self.sid, self.stop_flag

        def work():
            try:
                self.api.stream_chat(message, sid, lambda event: self.q.put(('chat', sid, event)), flag)
            except ApiError as exc:
                self.q.put(('chat', sid, {'type': 'error', 'error': str(exc)}))
            finally:
                self.q.put(('chat_end', sid))
        threading.Thread(target=work, daemon=True).start()
        self.dirty = True

    def stop_chat(self):
        if not self.streaming:
            return
        self.stop_flag.set()
        threading.Thread(target=self.api.cancel, args=(self.sid,), daemon=True).start()
        self.say('Stopping\u2026')

    def decide(self, approval, approved):
        if approval['id'] in self.busy_approvals:
            return
        self.busy_approvals.add(approval['id'])
        self.say('Approving\u2026' if approved else 'Declining\u2026')

        def work():
            try:
                result = self.api.post(f"/api/tool-approvals/{approval['id']}", {'approved': approved})
                self.q.put(('approval_done', approval['id'], str(result.get('answer', ''))))
            except ApiError as exc:
                self.q.put(('approval_error', approval['id'], str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def type_char(self, ch):
        if len(self.input) < 400:
            self.input += ch.upper() if self.shift else ch
            self.shift = False
        self.dirty = True

    def backspace(self):
        self.input = self.input[:-1]
        self.dirty = True

    def toggle(self, name):
        setattr(self, name, not getattr(self, name))
        self.dirty = True

    # ------------------------------------------------------------ events
    def handle_event(self, event):
        pg = self.pg
        if event.type == pg.QUIT:
            self.running = False
        elif event.type == pg.KEYDOWN:
            self.touch()
            if event.key == pg.K_ESCAPE:
                if self.typing:
                    self.typing = False
                elif self.page == 'task':
                    self.close_task()
                elif self.page != 'home':
                    self.open_page('home')
            elif self.page in ('chat', 'task') and self.typing:
                if event.key == pg.K_RETURN:
                    self.submit_input()
                elif event.key == pg.K_BACKSPACE:
                    self.backspace()
                elif event.unicode and event.unicode.isprintable():
                    self.input += event.unicode
        elif event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
            self.down, self.dragged = event.pos, False
            rect, _ = self.hit(event.pos)
            self.pressed = (rect.topleft + rect.size) if rect else None
            self.dirty = True
        elif event.type == pg.MOUSEMOTION and event.buttons[0] and self.down:
            if abs(event.pos[1] - self.down[1]) > self.S(14):
                self.dragged = True
            if self.dragged and self.page == 'chat':
                self.scroll = max(0, self.scroll + event.rel[1])
                self.dirty = True
        elif event.type == pg.MOUSEBUTTONUP and event.button == 1:
            woke = self.mode != 'awake'
            self.booted = True
            self.touch()
            tapped, self.pressed, self.down = not self.dragged, None, None
            self.dirty = True
            if woke or not tapped:
                return
            _, action = self.hit(event.pos)
            if action:
                action()

    def drain(self):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            self.dirty = True
            kind = item[0]
            if kind == 'wall':
                self.wall, self.online, self.error = item[1], True, ''
                self.wall_at = time.time()
                if len(self.wall.get('alerts', [])) > len(self.ringing):
                    self.touch()
                self.ringing = list(self.wall.get('alerts', []))
                count = len(self.wall.get('approvals', []))
                if count > self.prev_approvals:
                    self.touch()
                self.prev_approvals = count
            elif kind == 'event':
                self.on_event(item[1])
            elif kind == 'voice':
                new, old = item[1], self.voice or {}
                if new.get('asleep') and not old.get('asleep'):
                    self.sleeping = True                      # "goodnight" to the speaker also puts this screen to sleep
                elif old.get('asleep') and not new.get('asleep'):
                    self.touch()                              # "good morning" or an alarm wakes it
                elif not new.get('asleep') and new.get('ring') in ('listening', 'thinking', 'speaking', 'alert') and new.get('ring') != old.get('ring'):
                    self.touch()
                self.voice = new
            elif kind == 'offline':
                self.online, self.error, self.error_status = False, item[1], item[2]
            elif kind == 'chat' and item[1] == self.sid and self.chat:
                self.on_chat_event(item[2])
            elif kind == 'chat_end' and item[1] == self.sid:
                self.streaming = False
                reply = self.chat[-1] if self.chat else None
                if reply and reply['role'] == 'assistant' and not reply['text'].strip():
                    reply['text'] = '(no reply)'
                if reply:
                    reply.pop('status', None)
                self.poller.kick.set()
            elif kind == 'task_sent':
                if self.task and self.task.get('id') == item[1]:
                    self.close_task()
                if self.wall:
                    self.wall['needs_input'] = [n for n in self.wall.get('needs_input', []) if n.get('id') != item[1]]
                self.say('Marked done' if item[2] == 'done' else 'Sent to Sage. It continues soon.', 4)
                self.poller.kick.set()
            elif kind == 'approval_done':
                self.busy_approvals.discard(item[1])
                if self.wall:
                    self.wall['approvals'] = [a for a in self.wall.get('approvals', []) if a['id'] != item[1]]
                self.say(plain(item[2])[:90] or 'Done')
                self.poller.kick.set()
            elif kind == 'approval_error':
                self.busy_approvals.discard(item[1])
                self.say('Could not send: ' + item[2][:80], 5)

    def on_event(self, event):
        """React to something that just happened on the server."""
        kind, data, level = event.get('kind', ''), event.get('data') or {}, event.get('level', 'info')
        if kind in ('timer.done', 'reminder.due'):
            if not any(r.get('id') == data.get('id') for r in self.ringing):
                self.ringing.append({'id': data.get('id'), 'kind': data.get('kind', 'reminder'), 'title': data.get('title') or event.get('title', '')})
            self.touch()
        elif kind in ('reminder.done', 'reminder.snoozed', 'reminder.cancelled'):
            self.ringing = [r for r in self.ringing if r.get('id') != data.get('id')]
        elif kind == 'user.awake':
            self.touch()                                          # your phone says you are up: wake the screen
        elif kind == 'brief.ready' and data.get('source') == 'awake' and data.get('text'):
            self.brief = {'text': str(data['text']), 'at': time.time()}
            self.page, self.typing = 'home', False
            self.touch()
        elif level in ('success', 'warning') and kind not in ('brief.ready',):
            self.say(event.get('title', '')[:80], 4)
        if kind.split('.')[0] in ('plan', 'task', 'project', 'job', 'reminder', 'timer', 'schedule', 'brief', 'approval'):
            self.poller.kick.set()
        self.dirty = True

    def seconds_left(self, timer):
        return max(0, int(timer.get('seconds_left', 0) - (time.time() - self.wall_at)))

    def ack(self, item, snooze=False):
        self.ringing = [r for r in self.ringing if r.get('id') != item.get('id')]
        self.dirty = True

        def work():
            try:
                self.api.post(f"/api/reminders/{item['id']}/{'snooze' if snooze else 'done'}", {'minutes': 10} if snooze else {}, 15)
            except ApiError as exc:
                self.q.put(('approval_error', -1, str(exc)))
            self.poller.kick.set()
        threading.Thread(target=work, daemon=True).start()

    def on_chat_event(self, event):
        reply = self.chat[-1]
        kind = event.get('type')
        if kind == 'token':
            reply['text'] += event.get('text', '')
            reply.pop('status', None)
        elif kind == 'status':
            reply['status'] = event.get('text', '')
        elif kind == 'reset':
            reply['text'] = ''
        elif kind == 'done':
            reply['text'] = plain(event.get('answer', '') or reply['text'])
            reply.pop('status', None)
        elif kind == 'error':
            reply['text'] = 'Error: ' + str(event.get('error', 'unknown'))
            reply.pop('status', None)
        self.scroll = 0 if self.scroll <= self.S(40) else self.scroll   # stay pinned to the newest text unless the user scrolled up

    # ------------------------------------------------------------ drawing: common
    def draw(self):
        if not self.booted:
            self.buttons = []
            self.draw_splash()
            return
        now = dt.datetime.now()
        self.mode = self.compute_mode(now)
        self.buttons = []
        if self.mode == 'clock':
            self.screen.fill((3, 5, 9))
            self.draw_clock(now)
            return
        self.background()
        if self.ringing:
            self.draw_alert()
        elif self.brief:
            self.draw_brief()
        elif self.page == 'chat':
            self.draw_chat()
        elif self.page == 'task' and self.task:
            self.draw_task()
        else:
            self.draw_home(now)
        if self.toast and time.time() < self.toast_until:
            S, p = self.S, self.p
            width = min(self.W - S(40), p.width(self.toast, S(21), 'medium') + S(52))
            rect = self.pg.Rect(0, 0, width, S(48))
            rect.midbottom = (self.W // 2, self.H - S(98))
            self.screen.blit(p.shadow_surface(rect.w, rect.h, S(24), S(16), 140), (rect.x - S(16), rect.y - S(10)))
            p.pill(rect, (44, 60, 92), (80, 104, 150))
            self.text(rect.centerx, rect.centery, self.toast, S(21), TEXT, 'medium', 'mc', max_w=rect.w - S(28))
        if self.mode == 'dim':
            if self.dim_overlay is None:
                self.dim_overlay = self.pg.Surface((self.W, self.H), self.pg.SRCALPHA)
                self.dim_overlay.fill((0, 0, 0, 120))
            self.screen.blit(self.dim_overlay, (0, 0))

    def draw_alert(self):
        """A timer or reminder is ringing: take over the whole screen until it is dismissed."""
        S, W, H, p = self.S, self.W, self.H, self.p
        item = self.ringing[0]
        pulse = 0.5 + 0.5 * math.sin(time.time() * 4)
        p.glow(W // 2, S(190), S(300), AMBER, int(60 + 60 * pulse))
        p.icon('bell', W // 2, S(150), S(92), AMBER)
        label = 'TIMER FINISHED' if item.get('kind') == 'timer' else ('ALARM' if item.get('kind') == 'alarm' else 'REMINDER')
        self.text(W // 2, S(236), label, S(20), AMBER, 'bold', 'mc')
        lines = p.wrap(item.get('title', ''), S(48), W - S(120), 'bold', max_lines=2)
        for i, line in enumerate(lines):
            self.text(W // 2, S(296) + i * S(58), line, S(48), TEXT, 'bold', 'mc')
        if len(self.ringing) > 1:
            self.text(W // 2, S(296) + len(lines) * S(58) + S(6), f'+{len(self.ringing) - 1} more', S(20), MUTED, 'regular', 'mc')
        bw, bh, y = S(280), S(84), H - S(150)
        self.button((W // 2 - bw - S(14), y, bw, bh), 'Snooze 10 min', lambda it=item: self.ack(it, True), 'secondary', True, 26)
        self.button((W // 2 + S(14), y, bw, bh), 'Done', lambda it=item: self.ack(it, False), 'success', True, 28, 'check')

    def dismiss_brief(self):
        self.brief, self.dirty = None, True

    def brief_lines(self, text, size, max_w):
        p, out = self.p, []
        for raw in text.split('\n'):
            raw = raw.rstrip()
            if not raw.strip():
                if out and out[-1][1] != 'gap':
                    out.append(('', 'gap'))
            elif raw.lstrip().startswith('\u2022'):
                lines = p.wrap(raw.lstrip()[1:].strip(), size, max_w - self.S(26), 'regular')
                out += [(line, 'bullet' if i == 0 else 'cont') for i, line in enumerate(lines)]
            elif raw.strip().endswith(':'):
                out += [(line, 'head') for line in p.wrap(raw.strip(), size, max_w, 'bold')]
            else:
                out += [(line, 'body') for line in p.wrap(raw.strip(), size, max_w, 'regular')]
        return out

    def draw_brief(self):
        """The wake-up brief your phone triggered: stays on screen until someone taps the screen."""
        S, W, H, p = self.S, self.W, self.H, self.p
        self.buttons.append((self.pg.Rect(0, 0, W, H), self.dismiss_brief))     # a tap anywhere closes it
        now = dt.datetime.now()
        p.orb(S(44), S(42), S(15), time.time(), glow=True)
        self.text(S(76), S(32), 'Your morning brief', S(30), TEXT, 'bold', 'ml')
        self.text(S(76), S(64), now.strftime('%A %d %B'), S(18), MUTED, 'regular', 'ml')
        self.text(W - S(32), S(38), now.strftime('%H:%M'), S(40), TEXT, 'light', 'mr')
        card = self.pg.Rect(S(24), S(92), W - S(48), H - S(92) - S(110))
        p.card(card, border=(52, 80, 128))
        body, inner_w, inner_h = plain(self.brief['text']), card.w - S(52), card.h - S(32)
        for size_try in (24, 22, 20, 18, 16):                         # biggest text that fits the whole brief
            sz = S(size_try)
            lines = self.brief_lines(body, sz, inner_w)
            line_h = int(sz * 1.36)
            if sum(line_h // 2 if kind == 'gap' else line_h for _, kind in lines) <= inner_h:
                break
        y, limit, x = card.y + S(16), card.bottom - S(16), card.x + S(26)
        for text, kind in lines:
            if kind == 'gap':
                y += line_h // 2
                continue
            if y + line_h > limit:
                self.text(x, y, '\u2026', sz, MUTED)
                break
            if kind == 'head':
                self.text(x, y, text, sz, TEAL, 'bold')
            elif kind in ('bullet', 'cont'):
                if kind == 'bullet':
                    self.pg.draw.circle(self.screen, ACCENT, (x + S(7), y + line_h // 2 - S(2)), S(3))
                self.text(x + S(26), y, text, sz, TEXT)
            else:
                self.text(x, y, text, sz, TEXT)
            y += line_h
        by = H - S(92)
        self.button((W // 2 - S(150), by, S(300), S(70)), 'Got it', self.dismiss_brief, 'primary', True, 26, 'check')
        self.text(W - S(32), by + S(35), 'tap anywhere to close', S(15), DIM, 'regular', 'mr')

    def draw_clock(self, now):
        S, W, H = self.S, self.W, self.H
        dim, dimmer = (92, 108, 138), (52, 62, 84)
        hh, mm = now.strftime('%H'), now.strftime('%M')
        size = S(230)
        font_w = self.p.width
        total = font_w(hh, size, 'light') + font_w(':', size, 'light') + font_w(mm, size, 'light')
        x = (W - total) // 2
        y = H // 2 - S(34)
        self.p.text(x, y, hh, size, dim, 'light', 'ml')
        x += font_w(hh, size, 'light')
        self.p.text(x, y - S(8), ':', size, (40, 82, 150), 'light', 'ml')
        x += font_w(':', size, 'light')
        self.p.text(x, y, mm, size, dim, 'light', 'ml')
        self.text(W // 2, H // 2 + S(104), now.strftime('%A %d %B'), S(28), dimmer, 'regular', 'mc')
        self.p.orb(W // 2, S(52), S(15), 0.0, glow=False)

    def header(self, now):
        S, p = self.S, self.p
        p.orb(S(40), S(34), S(13), 0.0, glow=False)
        self.text(S(66), S(34), 'Sage', S(26), TEXT, 'bold', 'ml')
        x = S(66) + p.width('Sage', S(26), 'bold') + S(16)
        if self.online:
            model = (self.wall or {}).get('model', '')
            ready = (self.wall or {}).get('ollama_online')
            label = model if ready else 'model offline: start Ollama'
            color = GREEN if ready else AMBER
        else:
            label, color = 'offline, retrying', RED
        width = p.width(label, S(16), 'medium') + S(42)
        chip = self.pg.Rect(x, S(34) - S(15), min(width, self.W - x - S(300)), S(30))
        p.pill(chip, (20, 31, 52), (40, 58, 90))
        self.pg.draw.circle(self.screen, color, (chip.x + S(15), chip.centery), S(5))
        self.text(chip.x + S(28), chip.centery, label, S(16), MUTED, 'medium', 'ml', max_w=chip.w - S(38))
        name = self.cfg.get('USER_NAME', '').strip()
        if self.online and name:
            hour = now.hour
            hello = 'Good night' if hour < 5 else 'Good morning' if hour < 12 else 'Good afternoon' if hour < 18 else 'Good evening'
            self.text(self.W - S(32), S(34), f'{hello}, {name}', S(22), TEXT, 'medium', 'mr')
        elif not self.online and self.error and self.wall:
            self.text(self.W - S(32), S(34), self.error[:48], S(15), RED, 'regular', 'mr')

    # ------------------------------------------------------------ drawing: home
    def draw_home(self, now):
        S, W, H, p = self.S, self.W, self.H, self.p
        wall = self.wall or {}
        self.header(now)
        if not self.online and not self.wall:
            self.draw_connect_help()
            self.draw_nav()
            return
        pad = S(32)
        left_w = S(392)
        # hero clock: soft light numerals with an accent colon
        size = S(132)
        hh, mm = now.strftime('%H'), now.strftime('%M')
        y0 = S(58)
        x = pad - S(4)
        p.text(x, y0, hh, size, TEXT, 'light')
        x += p.width(hh, size, 'light')
        p.text(x, y0 - S(6), ':', size, ACCENT, 'light')
        x += p.width(':', size, 'light')
        p.text(x, y0, mm, size, TEXT, 'light')
        date_y = y0 + int(size * 1.12)
        rect = self.text(pad, date_y, now.strftime('%A, %d %B'), S(26), MUTED, 'regular')
        y = rect.bottom + S(14)
        focus = wall.get('focus')
        if focus:
            pill = self.pg.Rect(pad, y, left_w, S(34))
            p.pill(pill, (52, 42, 96), (92, 78, 160))
            self.text(pill.x + S(16), pill.centery, 'Focus: ' + (focus.get('goal') or 'on'), S(17), TEXT, 'medium', 'ml', max_w=left_w - S(32))
            y += S(46)
        # today timeline
        p.icon('calendar', pad + S(10), y + S(11), S(20), ACCENT)
        self.text(pad + S(30), y + S(11), 'COMING UP', S(14), MUTED, 'bold', 'ml')
        y += S(32)
        events = self.upcoming_events(now)
        rows = [e for e in events[:4]]
        if not rows:
            self.text(pad + S(4), y + S(8), 'Nothing scheduled', S(21), MUTED, 'medium')
            self.text(pad + S(4), y + S(36), 'Enjoy the quiet.', S(17), DIM)
        row_h = S(56)
        usable = H - S(104) - y
        rows = rows[:max(1, usable // row_h)] if rows else []
        if len(rows) > 1:
            self.pg.draw.line(self.screen, (40, 56, 88), (pad + S(10), y + S(14)), (pad + S(10), y + row_h * (len(rows) - 1) + S(14)), S(2))
        today = now.date()
        for i, (start, end, event) in enumerate(rows):
            ry = y + i * row_h
            self.pg.draw.circle(self.screen, ACCENT if i == 0 else (60, 84, 130), (pad + S(10), ry + S(14)), S(6 if i == 0 else 5))
            if i == 0:
                self.pg.draw.circle(self.screen, (40, 82, 170), (pad + S(10), ry + S(14)), S(10), S(2))
            self.text(pad + S(34), ry + S(2), event.get('subject', ''), S(21), TEXT, 'medium', max_w=left_w - S(40))
            if event.get('is_all_day'):
                when = 'all day'
            elif start.date() == today:
                when = start.strftime('%H:%M')
            elif start.date() == today + dt.timedelta(days=1):
                when = 'tomorrow ' + start.strftime('%H:%M')
            else:
                when = start.strftime('%a %H:%M')
            sub = f"{when}  \u00b7  {relative_time(start, end, now)}"
            if event.get('location'):
                sub += f"  \u00b7  {event['location']}"
            self.text(pad + S(34), ry + S(28), sub, S(15), TEAL if i == 0 else MUTED, 'regular', max_w=left_w - S(40))
        # right column
        rx = pad + left_w + S(30)
        rw = W - rx - pad
        ry, bottom = S(72), H - S(94)
        gap = S(12)
        head = S(46)
        approvals, overdue = wall.get('approvals', []), wall.get('overdue', [])
        needs = wall.get('needs_input', [])
        if approvals or overdue or needs:
            body = (S(118) if approvals else 0) + S(32) * min(2, len(needs)) + S(28) * min(2, len(overdue))
            card = self.pg.Rect(rx, ry, rw, min(head + body + S(8), bottom - ry))
            p.card(card, border=(110, 88, 40) if (approvals or needs) else CARD_LINE, glow=AMBER if approvals else None)
            p.icon('bell', card.x + S(30), card.y + S(24), S(22), AMBER)
            self.text(card.x + S(54), card.y + S(24), 'NEEDS YOU', S(14), AMBER, 'bold', 'ml')
            cy = card.y + head
            if approvals:
                item = approvals[0]
                self.text(card.x + S(22), cy, f"Allow {item['tool']}?", S(26), TEXT, 'bold', max_w=rw - S(44))
                self.text(card.x + S(22), cy + S(36), item.get('summary') or 'Sage is waiting for your OK.', S(17), MUTED, max_w=rw - S(44))
                busy = item['id'] in self.busy_approvals
                bw = S(150)
                by = cy + S(58)
                self.button((card.right - S(22) - bw * 2 - S(12), by, bw, S(50)), 'Deny', lambda a=item: self.decide(a, False), 'danger', not busy, 21)
                self.button((card.right - S(22) - bw, by, bw, S(50)), 'Approve', lambda a=item: self.decide(a, True), 'success', not busy, 21, 'check')
                if len(approvals) > 1:
                    self.text(card.x + S(22), by + S(25), f'+{len(approvals) - 1} more waiting', S(16), AMBER, 'medium', 'ml')
                cy += S(118)
            for need in needs[:2]:
                row = self.pg.Rect(card.x + S(12), cy - S(2), rw - S(24), S(30))
                self.button(row, '', lambda n=need: self.open_task(n), 'ghost', True, 17)
                self.pg.draw.circle(self.screen, AMBER, (card.x + S(30), cy + S(13)), S(5))
                self.text(card.x + S(44), cy + S(13), f"{need['title']}", S(18), TEXT, 'medium', 'ml', max_w=rw - S(130))
                self.text(card.right - S(26), cy + S(13), 'Answer >', S(15), AMBER, 'bold', 'mr')
                cy += S(32)
            for task in overdue[:2]:
                self.pg.draw.circle(self.screen, RED, (card.x + S(28), cy + S(12)), S(5))
                self.text(card.x + S(44), cy, f"Overdue: {task['title']}", S(18), TEXT, 'regular', max_w=rw - S(66))
                cy += S(28)
            ry = card.bottom + gap
        timers = wall.get('timers', [])
        if timers and ry + head + S(30) * min(3, len(timers)) + S(8) <= bottom - S(80):
            shown = timers[:3]
            card = self.pg.Rect(rx, ry, rw, head + S(30) * len(shown) + S(8))
            p.card(card)
            p.icon('bell', card.x + S(30), card.y + S(24), S(20), ACCENT)
            self.text(card.x + S(54), card.y + S(24), 'TIMERS AND REMINDERS', S(14), MUTED, 'bold', 'ml')
            cy = card.y + head
            for timer in shown:
                left = self.seconds_left(timer)
                clock = f'{left // 3600}:{left % 3600 // 60:02d}:{left % 60:02d}' if left >= 3600 else f'{left // 60:02d}:{left % 60:02d}'
                is_timer = timer.get('kind') == 'timer'
                self.text(card.x + S(22), cy, timer.get('title', ''), S(18), TEXT, 'regular', max_w=rw - S(170))
                self.text(card.right - S(22), cy, clock if is_timer else timer.get('when', ''), S(20) if is_timer else S(15), TEAL if is_timer else MUTED, 'bold' if is_timer else 'regular', 'tr')
                cy += S(30)
            ry = card.bottom + gap
        working = wall.get('working', [])
        job_h = lambda j: S(46) if (j['status'] == 'running' and j.get('checkpoint')) else S(30)
        height = head + (sum(job_h(j) for j in working) if working else S(30)) + (S(32) if wall.get('queued') else 0) + S(8)
        if ry + height <= bottom:
            card = self.pg.Rect(rx, ry, rw, height)
            p.card(card)
            p.icon('spark', card.x + S(30), card.y + S(24), S(20), TEAL)
            self.text(card.x + S(54), card.y + S(24), 'SAGE IS WORKING ON', S(14), MUTED, 'bold', 'ml')
            cy = card.y + head
            if not working:
                recent = wall.get('recent_jobs', [])
                self.text(card.x + S(22), cy, ('All quiet. Last finished: ' + recent[0]['title']) if recent else 'All quiet', S(18), MUTED, 'regular', max_w=rw - S(44))
                cy += S(30)
            for job in working:
                running = job['status'] == 'running'
                self.pg.draw.circle(self.screen, GREEN if running else DIM, (card.x + S(28), cy + S(12)), S(5))
                self.text(card.x + S(44), cy, job['title'], S(18), TEXT if running else MUTED, 'medium' if running else 'regular', max_w=rw - S(66))
                cy += S(30)
                if running and job.get('checkpoint'):
                    self.text(card.x + S(44), cy - S(4), job['checkpoint'], S(14), TEAL, 'regular', max_w=rw - S(66))
                    cy += S(16)
            if wall.get('queued'):
                chip = self.pg.Rect(card.x + S(22), cy + S(2), S(104), S(24))
                p.pill(chip, (22, 34, 58), (44, 62, 96))
                self.text(chip.centerx, chip.centery, f"{wall['queued']} queued", S(14), MUTED, 'medium', 'mc')
            ry = card.bottom + gap
        tasks = wall.get('next_tasks', [])
        fit_rows = min(len(tasks), 3, max(0, (bottom - ry - head - S(8)) // S(30)))
        if fit_rows >= 1:
            card = self.pg.Rect(rx, ry, rw, head + S(30) * fit_rows + S(8))
            p.card(card)
            p.icon('checkbox', card.x + S(30), card.y + S(24), S(20), ACCENT)
            self.text(card.x + S(54), card.y + S(24), 'NEXT TASKS', S(14), MUTED, 'bold', 'ml')
            cy = card.y + head
            for task in tasks[:fit_rows]:
                project = task.get('project') or ''
                px = card.right - S(22)
                if project:
                    chip_w = p.width(project, S(13), 'medium') + S(20)
                    chip = self.pg.Rect(px - chip_w, cy + S(1), chip_w, S(24))
                    p.pill(chip, (22, 34, 58), (44, 62, 96))
                    self.text(chip.centerx, chip.centery, project, S(13), MUTED, 'medium', 'mc')
                    px = chip.x - S(10)
                self.text(card.x + S(22), cy, task['title'], S(18), TEXT, 'regular', max_w=px - card.x - S(30))
                cy += S(30)
            ry = card.bottom + gap
        last = wall.get('last_message')
        if last and bottom - ry > S(84):
            card = self.pg.Rect(rx, ry, rw, bottom - ry)
            p.card(card)
            p.icon('chat', card.x + S(30), card.y + S(26), S(20), MUTED)
            self.text(card.x + S(54), card.y + S(26), 'LAST MESSAGE' + (' FROM SAGE' if last['role'] == 'assistant' else ''), S(14), MUTED, 'bold', 'ml')
            max_lines = max(1, (card.h - S(58)) // S(24))
            for i, line in enumerate(p.wrap(plain(last['content']), S(17), rw - S(44), max_lines=max_lines)):
                self.text(card.x + S(22), card.y + S(48) + i * S(24), line, S(17), TEXT)
        self.draw_nav()

    def draw_connect_help(self):
        S, W, H, p = self.S, self.W, self.H, self.p
        cx = W // 2
        p.orb(cx, S(138), S(48), time.time(), RED if self.error else ACCENT)
        self.text(cx, S(236), 'Cannot reach Sage', S(34), TEXT, 'bold', 'mc')
        self.text(cx, S(272), f'Looking for {self.api.base}', S(19), MUTED, 'regular', 'mc')
        card = self.pg.Rect(cx - S(380), S(300), S(760), S(176))
        p.card(card)
        self.text(card.x + S(26), card.y + S(20), self.error or 'Connecting\u2026', S(16), RED if self.error else MUTED, 'medium', max_w=card.w - S(52))
        if self.error_status == 403:
            steps = ["Sage refused this device (403).", "Add this Pi's IP to SAGE_ALLOWED_CLIENT_IPS on the PC and restart Sage."]
        else:
            steps = ['Start Sage on the PC (start-sage.bat listens on the network).',
                     "Add this Pi's IP to SAGE_ALLOWED_CLIENT_IPS and restart Sage.",
                     'Check SAGE_URL in /etc/sage-wall.conf and that the firewall allows port 8001.']
        y = card.y + S(54)
        for i, step in enumerate(steps, 1):
            self.pg.draw.circle(self.screen, (34, 58, 112), (card.x + S(40), y + S(13)), S(12))
            self.text(card.x + S(40), y + S(13), str(i), S(14), TEXT, 'bold', 'mc')
            self.text(card.x + S(64), y + S(13), step, S(17), TEXT, 'regular', 'ml', max_w=card.w - S(92))
            y += S(38)

    def draw_nav(self):
        S, W, H = self.S, self.W, self.H
        y, h, gap, pad = H - S(82), S(62), S(12), S(32)
        busy = self.streaming
        self.button((pad, y, S(290), h), 'Ask Sage', lambda: (self.open_page('chat'), setattr(self, 'typing', True)), 'primary', True, 25, 'spark')
        x = pad + S(290) + gap
        for label, prompt in (('Brief', 'Give me a short daily briefing using my saved Sage project and task records. Keep it under 100 words.'),
                              ('Next action', 'What is the single best next action across my active projects? Keep it under 60 words.'),
                              ('Status', 'Give me a concise status update on my active projects. Keep it under 100 words.')):
            self.button((x, y, S(138), h), label, lambda q=prompt: self.start_chat(q), 'secondary', not busy, 21)
            x += S(138) + gap
        self.draw_ring(x + (W - pad - S(62) - x) // 2, y + S(26), S(17))
        self.button((W - pad - S(62), y, S(62), h), '', self.go_sleep, 'ghost', True, 21, 'moon')

    def draw_ring(self, cx, cy, r):
        """Mirror of the speaker's RGB light ring: seven dots whose colour and motion show what the speaker is doing."""
        v = self.voice
        if not v or not v.get('device_online'):
            return
        ring, t = v.get('ring', 'idle'), time.time()
        palette = {'idle': (70, 96, 150), 'listening': ACCENT, 'thinking': AMBER, 'speaking': GREEN, 'alert': AMBER,
                   'muted': RED, 'error': RED, 'sleeping': (40, 52, 80)}
        base = palette.get(ring, MUTED)
        for i in range(7):
            ang = -math.pi / 2 + i * 2 * math.pi / 7
            if ring == 'thinking':
                k = 1 - ((t * 1.6 - i / 7) % 1.0)                       # one bright dot circling
            elif ring == 'speaking':
                k = 0.55 + 0.45 * math.sin(t * 6 + i * 0.9)
            elif ring == 'listening':
                k = 0.7 + 0.3 * math.sin(t * 3)
            elif ring == 'alert':
                k = 1.0 if int(t * 2) % 2 == 0 else 0.25
            elif ring == 'idle':
                k = 0.55
            elif ring == 'sleeping':
                k = 0.35
            else:
                k = 1.0
            k = max(0.12, min(1.0, k))
            x, y = int(cx + math.cos(ang) * r), int(cy + math.sin(ang) * r)
            if k > 0.6:
                self.p.glow(x, y, self.S(9), base, 70 if k > 0.85 else 40)
            self.pg.draw.circle(self.screen, mix((14, 22, 38), base, k), (x, y), self.S(4))
        self.text(cx, cy + r + self.S(14), 'sleep' if ring == 'sleeping' else ring, self.S(12), MUTED, 'regular', 'mc')

    def go_sleep(self):
        self.sleeping, self.page, self.dirty = True, 'home', True

    # ------------------------------------------------------------ drawing: chat
    def draw_task(self):
        """A task that needs the user: what it needs, and a way to answer Sage by touch."""
        S, W, H, p = self.S, self.W, self.H, self.p
        task = self.task
        mine = task.get('kind') == 'physical'
        pad = S(14)
        self.button((pad, S(8), S(52), S(52)), '', self.close_task, 'ghost', True, 20, 'chevron')
        self.text(W // 2, S(34), 'You do this' if mine else 'Sage needs your input', S(24), TEXT, 'bold', 'mc')
        bottom_h = S(306) if self.typing else S(96)
        card = self.pg.Rect(S(24), S(76), W - S(48), H - S(76) - bottom_h - S(12))
        p.card(card, border=(110, 88, 40))
        y = card.y + S(18)
        for line in p.wrap(task.get('title', ''), S(28), card.w - S(44), 'bold', max_lines=2):
            self.text(card.x + S(22), y, line, S(28), TEXT, 'bold')
            y += S(34)
        self.text(card.x + S(22), y + S(2), f"{task.get('project') or 'no project'}  \u00b7  {task.get('kind', '')}", S(16), MUTED)
        y += S(34)
        self.text(card.x + S(22), y, 'WHAT YOU NEED TO DO' if mine else 'WHAT SAGE NEEDS', S(14), AMBER, 'bold')
        y += S(24)
        rows = max(1, (card.bottom - y - S(12)) // S(26))
        for line in p.wrap(task.get('question') or 'Tell Sage what to do next.', S(20), card.w - S(44), max_lines=rows):
            self.text(card.x + S(22), y, line, S(20), TEXT)
            y += S(26)
        if self.typing:
            self.draw_keyboard(H - bottom_h)
        else:
            by = H - S(84)
            if mine:
                self.button((pad + S(10), by, S(330), S(68)), 'I did it', lambda: self.send_task_input('', True), 'success', True, 25, 'check')
                self.button((pad + S(350), by, S(330), S(68)), 'Add a note', lambda: setattr(self, 'typing', True), 'secondary', True, 22, 'chat')
            else:
                self.button((pad + S(10), by, W - pad * 2 - S(20), S(68)), 'Reply to Sage', lambda: setattr(self, 'typing', True), 'primary', True, 25, 'chat')

    def draw_chat(self):
        S, W, H, p = self.S, self.W, self.H, self.p
        pad = S(14)
        self.button((pad, S(8), S(52), S(52)), '', lambda: (setattr(self, 'typing', False), self.open_page('home')), 'ghost', True, 20, 'chevron')
        self.text(W // 2 + S(16), S(34), 'Ask Sage', S(26), TEXT, 'bold', 'mc')
        p.orb(W // 2 - S(66), S(34), S(13), time.time() if self.streaming else 0.0, glow=self.streaming)
        if self.streaming:
            self.button((W - pad - S(132), S(10), S(132), S(48)), 'Stop', self.stop_chat, 'danger', True, 20, 'stop')
        else:
            self.button((W - pad - S(110), S(10), S(110), S(48)), 'Clear', lambda: (self.chat.clear(), setattr(self, 'scroll', 0)), 'ghost', bool(self.chat), 19)
        bottom_h = S(306) if self.typing else S(88)
        area = self.pg.Rect(0, S(68), W, H - S(68) - bottom_h)
        self.screen.set_clip(area)
        max_w = int(W * 0.72) - S(40)
        blocks, total = [], 0
        for message in self.chat:
            body = plain(message['text'])
            thinking = (message.get('status') and not body)
            lines = [' '] if thinking else p.wrap(body or ' ', S(20), max_w)
            height = (S(46) if thinking else len(lines) * S(27) + S(26))
            blocks.append((message, lines, height, thinking))
            total += height + S(12)
        self.scroll = max(0, min(self.scroll, max(0, total - area.h + S(16))))
        y = area.bottom - S(8) + self.scroll
        if not self.chat:
            p.orb(W // 2, area.centery - S(20), S(34), time.time(), glow=True)
            self.text(W // 2, area.centery + S(54), 'Ask me anything', S(24), MUTED, 'medium', 'mc')
        for message, lines, height, thinking in reversed(blocks):
            y -= height
            mine = message['role'] == 'user'
            text_w = max((p.width(line, S(20)) for line in lines), default=0)
            width = max(S(70), min(max_w + S(36), text_w + S(36)))
            x = W - pad - width if mine else pad + S(40)
            rect = self.pg.Rect(x, y, width, height)
            if rect.bottom > area.top:
                if mine:
                    self.screen.blit(p.gradient_rrect(rect.w, rect.h, S(20), (60, 116, 238), (48, 168, 200)), rect.topleft)
                else:
                    self.screen.blit(p.rrect_surface(rect.w, rect.h, S(20), (22, 34, 56), (40, 58, 90), 1), rect.topleft)
                    p.orb(pad + S(18), rect.y + S(20), S(11), 0.0, glow=False)
                if thinking:
                    for i in range(3):
                        phase = (math.sin(time.time() * 5 - i * 0.9) + 1) / 2
                        self.pg.draw.circle(self.screen, mix((70, 90, 130), TEXT, phase), (rect.x + S(24) + i * S(16), rect.centery), S(5))
                else:
                    last = message is self.chat[-1] and self.streaming and not mine
                    for i, line in enumerate(lines):
                        shown = line + (' |' if last and i == len(lines) - 1 and int(time.time() * 2) % 2 else '')
                        self.text(rect.x + S(18), rect.y + S(13) + i * S(27), shown, S(20), TEXT)
            y -= S(12)
        self.screen.set_clip(None)
        if self.typing:
            self.draw_keyboard(H - bottom_h)
        else:
            by = H - S(80)
            self.button((pad, by, W - pad * 2 - S(250), S(64)), 'Type a message', lambda: setattr(self, 'typing', True), 'secondary', not self.streaming, 22, 'chat')
            self.button((W - pad - S(238), by, S(112), S(64)), 'Brief', lambda: self.start_chat('Give me a short daily briefing using my saved Sage project and task records. Keep it under 100 words.'), 'ghost', not self.streaming, 19)
            self.button((W - pad - S(118), by, S(118), S(64)), 'Next', lambda: self.start_chat('What is the single best next action across my active projects? Keep it under 60 words.'), 'ghost', not self.streaming, 19)

    def draw_keyboard(self, top):
        S, W, p = self.S, self.W, self.p
        pad, gap = S(8), S(6)
        field = self.pg.Rect(pad + S(4), top + S(2), W - pad * 2 - S(8), S(50))
        self.screen.blit(p.rrect_surface(field.w, field.h, S(16), (14, 22, 38), (50, 70, 108), 1), field.topleft)
        shown = self.input
        while shown and p.width(shown + '|', S(23)) > field.w - S(36):
            shown = shown[1:]
        if self.input:
            blink = '|' if int(time.time() * 2) % 2 else ' '
            self.text(field.x + S(18), field.centery, shown + blink, S(23), TEXT, 'regular', 'ml')
        else:
            self.text(field.x + S(18), field.centery, 'Message Sage', S(23), DIM, 'regular', 'ml')
        if self.symbols:
            rows = [list('1234567890'), list('-/:;()&@"'), list(".,?!'%+=")]
        else:
            rows = [list('qwertyuiop'), list('asdfghjkl'), list('zxcvbnm')]
        ku = (W - pad * 2 - gap * 9) / 10
        kh = S(52)
        y = top + S(2) + S(50) + gap

        def unit_w(units):
            return int(units * ku + (units - 1) * gap)
        for index, row in enumerate(rows):
            keys = [(ch, 1, (lambda c=ch: self.type_char(c)), 'key', None) for ch in row]
            if index == 2:
                if not self.symbols:
                    keys.insert(0, ('', 1.5, lambda: self.toggle('shift'), 'primary' if self.shift else 'special', 'shift'))
                keys.append(('Del', 1.5, self.backspace, 'special', None))
            total = sum(unit_w(u) for _, u, _, _, _ in keys) + gap * (len(keys) - 1)
            x = (W - total) // 2
            for label, units, action, kind, icon in keys:
                shown_label = label.upper() if (self.shift and len(label) == 1) else label
                self.button((x, y, unit_w(units), kh), shown_label, action, kind, True, 25 if len(label) == 1 else 19, icon)
                x += unit_w(units) + gap
            y += kh + gap
        has_text = bool(self.input.strip())
        bottom_keys = [('abc' if self.symbols else '123', 1.5, lambda: self.toggle('symbols'), 'special', None),
                       (',', 1, lambda: self.type_char(','), 'key', None), ('Space', 5, lambda: self.type_char(' '), 'key', None),
                       ('.', 1, lambda: self.type_char('.'), 'key', None),
                       ('Send', 1.5, lambda: self.submit_input(), 'primary' if has_text else 'special', 'send')]
        total = sum(unit_w(u) for _, u, _, _, _ in bottom_keys) + gap * (len(bottom_keys) - 1)
        x = (W - total) // 2
        for label, units, action, kind, icon in bottom_keys:
            self.button((x, y, unit_w(units), kh), label, action, kind, has_text if label == 'Send' else True, 19 if len(label) > 1 else 25, icon)
            x += unit_w(units) + gap

    # ------------------------------------------------------------ drawing: splash
    def draw_splash(self):
        S, W, H, p = self.S, self.W, self.H, self.p
        t = time.time() - self.boot_t0
        fade = min(1.0, t / 0.9)
        self.background()
        for fx, fy, phase, size in self.stars:
            twinkle = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * 1.3 + phase))
            c = mix(BG_BOT, (190, 210, 255), twinkle * 0.7 * fade)
            self.pg.draw.circle(self.screen, c, (int(fx * W), int(fy * H)), max(1, int(size * H / 600)))
        cx, cy = W // 2, H // 2 - S(92)
        # expanding rings
        if self.ring_layer is None:
            self.ring_layer = self.pg.Surface((S(520), S(520)), self.pg.SRCALPHA)
        layer = self.ring_layer
        layer.fill((0, 0, 0, 0))
        for i in range(3):
            phase = ((t * 0.45) + i / 3.0) % 1.0
            radius = int(S(118) + phase * S(130))
            self.pg.draw.circle(layer, (*ACCENT, int(70 * (1 - phase) * fade)), (S(260), S(260)), radius, max(1, S(2)))
        self.screen.blit(layer, (cx - S(260), cy - S(260)))
        p.orb(cx, cy, S(104), t)
        title = 'S  A  G  E'
        self.text(cx, cy + S(172), title, S(60), mix(BG_BOT, TEXT, fade), 'bold', 'mc')
        self.pg.draw.line(self.screen, mix(BG_BOT, ACCENT, fade), (cx - S(70), cy + S(212)), (cx + S(70), cy + S(212)), S(2))
        self.text(cx, cy + S(238), 'Self-Aware Generative Entity', S(19), mix(BG_BOT, MUTED, fade), 'regular', 'mc')
        host = self.api.base.split('//')[-1]
        steps = [('Display ready', True), (f'Connecting to Sage at {host}', self.online), ('Loading your day', self.wall is not None)]
        x0 = cx - S(150)
        y = H - S(92)
        for label, done in steps:
            if done:
                self.pg.draw.circle(self.screen, (24, 90, 66), (x0, y + S(10)), S(9))
                p.icon('check', x0, y + S(10), S(12), GREEN)
            else:
                self.pg.draw.circle(self.screen, (60, 78, 112), (x0, y + S(10)), S(8), S(2))
            self.text(x0 + S(24), y + S(10), label, S(16), GREEN if done else MUTED, 'medium' if done else 'regular', 'ml', max_w=S(400))
            y += S(26)
        if not self.online and t > 3 and self.error:
            self.text(cx, H - S(16), 'Waiting for Sage \u00b7 ' + self.error[:60], S(14), AMBER, 'regular', 'mc')

    # ------------------------------------------------------------ loop
    def tick_state(self):
        if not self.booted:
            elapsed = time.time() - self.boot_t0
            if (self.wall is not None and elapsed >= 3.2) or elapsed >= 7.0:
                self.booted = True
            self.dirty = True
        now = dt.datetime.now()
        key = (now.hour, now.minute, self.compute_mode(now))
        if key != self.minute_key:
            self.minute_key, self.dirty = key, True
        if self.toast and time.time() >= self.toast_until:
            self.toast, self.dirty = '', True
        if self.streaming or self.typing:
            self.dirty = True
        if (self.wall and self.wall.get('timers') and self.mode == 'awake' and self.page == 'home') or self.ringing:
            if int(time.time()) != self._sec:
                self._sec = int(time.time())
                self.dirty = True
        if self.mode == 'awake' and self.page == 'home' and self.voice and self.voice.get('device_online') \
                and self.voice.get('ring') in ('listening', 'thinking', 'speaking', 'alert', 'error'):
            self.dirty = True                                           # animate the ring only while it is active

    def frame(self):
        for event in self.pg.event.get():
            self.handle_event(event)
        self.drain()
        self.tick_state()
        if self.dirty:
            self.draw()
            self.pg.display.flip()
            self.dirty = False

    def run(self):
        self.poller.start()
        self.listener.start()
        self.voice_listener.start()
        clock = self.pg.time.Clock()
        while self.running:
            self.frame()
            clock.tick(20 if not self.booted else 12 if (self.streaming or self.typing or self.down) else 6)
        self.poller.halt.set()
        self.listener.halt.set()
        self.voice_listener.halt.set()


def main():
    parser = argparse.ArgumentParser(description='Sage wall display for a Raspberry Pi with a 7-inch touch screen')
    parser.add_argument('--config', help='path to a sage-wall.conf file')
    parser.add_argument('--url', help='Sage server URL, e.g. http://192.168.1.10:8001')
    parser.add_argument('--demo', action='store_true', help='use fake data instead of a server')
    parser.add_argument('--windowed', metavar='WxH', help='run in a window, e.g. 1024x600 (for testing on a PC)')
    parser.add_argument('--screenshot', metavar='PNG', help='render one frame to a PNG and exit (headless test)')
    parser.add_argument('--page', default='home', choices=['home', 'chat', 'keyboard', 'streaming', 'clock', 'splash', 'offline', 'alert', 'task', 'taskkb', 'brief'], help='page for --screenshot')
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.url:
        cfg['SAGE_URL'] = args.url
    if args.screenshot:
        os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
    import pygame
    pygame.init()
    pygame.display.set_caption('Sage wall')
    size = (1024, 600)
    if args.windowed or args.screenshot:
        try:
            size = tuple(int(v) for v in (args.windowed or '1024x600').lower().split('x'))
        except ValueError:
            pass
        screen = pygame.display.set_mode(size)
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
    api = DemoApi() if args.demo else Api(cfg['SAGE_URL'])
    app = WallApp(cfg, api, screen, pygame)
    if args.demo:
        app.voice = {'version': 0, 'ring': 'speaking' if args.screenshot else 'idle', 'asleep': False, 'device_online': True}
    if args.screenshot:
        app.poller.start()
        deadline = time.time() + 8
        while time.time() < deadline and not app.wall and not app.error:
            time.sleep(0.1)
            app.drain()
        if args.page in ('chat', 'keyboard', 'streaming'):
            app.page = 'chat'
            app.chat = [{'role': 'user', 'text': 'What should I do next on the ROV?'},
                        {'role': 'assistant', 'text': 'Finish the RS485 framing notes first, then order the depth sensor so wiring can start next week.'}]
            if args.page == 'keyboard':
                app.typing, app.input = True, 'Make a project for an export plugin'
            if args.page == 'streaming':
                app.chat += [{'role': 'user', 'text': 'Can you make a project for an Export plugin?'},
                             {'role': 'assistant', 'text': 'Sure. I will create the project and add a first task to define the plugin inputs and the export', 'status': None}]
                app.streaming = True
        if args.page == 'clock':
            app.sleeping = True
            if app.wall:
                app.wall['approvals'] = []
        app.booted = args.page != 'splash'
        if args.page == 'splash':
            app.boot_t0 = time.time() - 2.1
            app.online, app.wall = True, None
        if args.page in ('task', 'taskkb') and app.wall:
            app.open_task(app.wall['needs_input'][0])
            if args.page == 'taskkb':
                app.typing, app.input = True, 'Use the MS5837, it is waterproof'
        if args.page == 'brief':
            app.brief = {'text': "Good morning, Kyan. Here is your brief for Friday 16 October.\n\nCalendar:\n- 07:00  Thermodynamics lecture\n- 10:30  ROV: test RS485 link\n- 14:00  Dinner with Elles\n\nReminders and timers today:\n- 09:00  Call the dentist\n\nSage finished since yesterday:\n- Project task: write RS485 framing notes: Section 2 drafted and checked\n\nSage's queue today (about 45 min of work):\n1. Define the export plugin inputs (Export Plugin)\n2. Wire the second Arduino Nano (ROV)\n\nYour tasks: Order depth sensor; Choose the PCB colour\n\nOverdue: Order depth sensor", 'at': time.time()}
        if args.page == 'alert':
            app.ringing = [{'id': 9, 'kind': 'reminder', 'title': 'Call the dentist'}]
        if args.page == 'offline':
            app.wall, app.online, app.error = None, False, '[Errno 111] Connection refused'
        app.draw()
        pygame.image.save(screen, args.screenshot)
        print('saved', args.screenshot)
        return 0
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    pygame.quit()
    return 0


if __name__ == '__main__':
    sys.exit(main())

