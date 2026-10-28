"""Local Linux command, package, and X11 desktop controls."""
import os
import re
import shutil
import signal
import subprocess
import threading
import tempfile
import base64
import json
import urllib.parse
import uuid
import time
from pathlib import Path
from . import config

_OUTPUT_LIMIT = 12_000


def _linux_user():
    if not __import__('sys').platform.startswith('linux'):
        raise RuntimeError('These controls are available only on the Linux Sage host.')
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise PermissionError('Sage must run as your normal Linux user, not as root.')


def _run(command, cwd=None, timeout=90, cancel=None):
    """Run one already-approved process, bound its lifetime/output, and kill its process group on timeout."""
    proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, close_fds=True)
    output = {'stdout': bytearray(), 'stderr': bytearray(), 'truncated': False}

    def drain(stream, target):
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            room = _OUTPUT_LIMIT - len(output[target])
            if room > 0:
                output[target].extend(chunk[:room])
            if len(chunk) > room:
                output['truncated'] = True

    readers = [threading.Thread(target=drain, args=(proc.stdout, 'stdout'), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 'stderr'), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + max(1, min(int(timeout), 900))
    while proc.poll() is None:
        if cancel is not None and cancel.cancelled:
            cancelled = True
        remaining = deadline - time.monotonic()
        if cancelled or remaining <= 0:
            timed_out = remaining <= 0 and not cancelled
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
            break
        try: proc.wait(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired: pass
    for reader in readers:
        reader.join(timeout=2)
    decode = lambda data: data.decode('utf-8', 'replace')
    return {
        'returncode': proc.returncode,
        'stdout': decode(output['stdout']),
        'stderr': decode(output['stderr']),
        'truncated': output['truncated'],
        'timed_out': timed_out,
        'cancelled': cancelled,
    }


def run_command(arguments, approved=False, cancel=None):
    _linux_user()
    command = str(arguments.get('command', '')).strip()
    if not command or len(command) > 4000 or '\x00' in command:
        raise ValueError('Enter one Linux command of 1–4000 characters.')
    cwd_text = str(arguments.get('cwd', '')).strip()
    cwd = Path(cwd_text).expanduser().resolve() if cwd_text else Path.home()
    if not cwd.is_dir():
        raise ValueError('The requested working directory does not exist or is not a directory.')
    timeout = arguments.get('timeout_seconds', 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise ValueError('timeout_seconds must be an integer from 1 to 900.')
    if requires_install_approval(command) and not approved:
        raise PermissionError('Software installation or download commands must be approved in Sage before execution.')
    if requires_source_approval(command,cwd) and not approved:
        raise PermissionError('Changes to Sage source files must be approved in Sage before execution.')
    # For ordinary commands, use systemd's mount namespace to make Sage's source
    # tree genuinely read-only. Fail closed if the user service manager cannot
    # provide that boundary; never silently run an unrestricted shell.
    systemd_run = shutil.which('systemd-run')
    if not approved:
        if not systemd_run:
            raise RuntimeError('Safe command execution needs systemd-run so Sage can keep its own files read-only.')
        unit = 'sage-command-' + uuid.uuid4().hex[:12]
        source_root = _sage_root().resolve()
        isolated = [systemd_run, '--user', '--unit=' + unit, '--pipe', '--wait', '--collect', '--quiet',
                    '--property=ReadOnlyPaths=' + str(source_root)]
        # The project checkout contains data/ by default. Keep source read-only
        # while explicitly reopening configured user data folders nested inside it.
        for writable in (config.DATA, config.VAULT, config.WORKSPACE, source_root / 'outputs', source_root / 'work'):
            path = writable.expanduser().resolve()
            try: nested = path != source_root and source_root in path.parents
            except (OSError, ValueError): nested = False
            if nested and path.exists(): isolated.append('--property=ReadWritePaths=' + str(path))
        isolated += [
                    '--working-directory=' + str(cwd), '/bin/bash', '-c', command]
        result = _run(isolated, timeout=timeout, cancel=cancel)
        if result['timed_out'] or result['cancelled']:
            manager = shutil.which('systemctl')
            if manager:
                subprocess.run([manager, '--user', 'stop', unit + '.service'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result['returncode'] and 'Failed to connect to bus' in (result['stderr'] or ''):
            raise RuntimeError('Sage could not reach the user systemd manager needed to protect its own files. Log in to the Kali desktop once, then restart Sage.')
    else:
        result = _run(['/bin/bash', '-c', command], cwd=str(cwd), timeout=timeout, cancel=cancel)
    result['cwd'] = str(cwd)
    return result


def _sage_root():
    # system_control.py lives in <Sage root>/sage/.
    return Path(__file__).resolve().parent.parent


def is_sage_source_path(path):
    """Whether a path belongs to Sage's application code/configuration rather than its user data."""
    candidate = Path(path).expanduser().resolve()
    root = _sage_root().resolve()
    code_roots = [root / name for name in ('sage','plugins','scripts','wall','tests','.vscode')]
    code_roots.append(config.PLUGIN_DIR.expanduser().resolve())
    if candidate == root or candidate.parent == root: return True
    return any(candidate == item or item in candidate.parents for item in code_roots)


def _x11_display_size():
    xdotool = shutil.which('xdotool')
    if not xdotool or not os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'): return None
    try:
        r = subprocess.run([xdotool, 'getdisplaygeometry'], capture_output=True, text=True, timeout=2, check=True)
        width, height = (int(value) for value in r.stdout.split())
        return {'width': width, 'height': height}
    except Exception:
        return None


_INSTALL_MARKERS = re.compile(
    r'(?i)(?:^|[;&|\s])(?:(?:sudo|pkexec)\s+)?(?:apt(?:-get)?\s+(?:install|reinstall|build-dep|download|upgrade|full-upgrade|dist-upgrade|remove|purge)|dpkg\s+-i|pipx?\s+install|pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+(?:pip\s+install|tool\s+install)|poetry\s+add|npm\s+(?:install|i)|yarn\s+add|pnpm\s+add|bun\s+add|gem\s+install|cargo\s+install|go\s+install|flatpak\s+install|snap\s+install|ollama\s+pull|conda\s+install|mamba\s+install|brew\s+install|curl\b|wget\b)\b'
)
_SOURCE_MUTATOR = re.compile(r'(?i)(?:>|(?:^|[\s;&|])(?:tee|sed\s+-i|perl\s+-i|nano|vim|vi|emacs|cp|mv|rm|install|truncate|touch|python(?:3)?|perl|ruby|cat|bash|sh)\b)')


def requires_install_approval(command):
    return bool(_INSTALL_MARKERS.search(str(command)))


def requires_source_approval(command, cwd=None):
    text = str(command)
    normalized = text.replace('\\','/').lower()
    if cwd is not None and is_sage_source_path(cwd) and _SOURCE_MUTATOR.search(text): return True
    root = _sage_root().resolve()
    protected = [root / name for name in ('sage','plugins','scripts','wall','tests','.vscode')]
    protected.append(config.PLUGIN_DIR.expanduser().resolve())
    protected.extend(p for p in root.iterdir() if p.is_file())
    for item in protected:
        candidates = [item.as_posix().lower()]
        try: candidates.append(item.relative_to(root).as_posix().lower())
        except ValueError: pass
        if any(candidate in normalized for candidate in candidates) and _SOURCE_MUTATOR.search(text): return True
    return False


def inspect_screen(question='Describe the current desktop, focusing on visible app and dialog state.', include_preview=False):
    """Capture one transient desktop frame and ask local Ollama vision to describe it."""
    _linux_user()
    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        raise RuntimeError('No graphical session is available to capture.')
    capture = shutil.which('grim') if os.environ.get('WAYLAND_DISPLAY') else shutil.which('scrot')
    if not capture:
        utility = 'grim' if os.environ.get('WAYLAND_DISPLAY') else 'scrot'
        raise RuntimeError(f'Sage needs the {utility} screenshot utility. Install it from Sage’s approval card first.')
    with tempfile.TemporaryDirectory(prefix='sage-screen-') as folder:
        image_path = Path(folder) / ('frame.png' if capture.endswith('grim') else 'frame.jpg')
        argv = [capture, str(image_path)] if capture.endswith('scrot') else [capture, str(image_path)]
        if capture.endswith('scrot'): argv.insert(1, '-q'); argv.insert(2, '65')
        result = _run(argv, timeout=15)
        if result['returncode'] or not image_path.is_file():
            raise RuntimeError((result['stderr'] or 'Screenshot capture failed').strip()[:500])
        raw = image_path.read_bytes()
        if not raw or len(raw) > 12_000_000:
            raise RuntimeError('The screenshot is empty or larger than 12 MB.')
        image = base64.b64encode(raw).decode('ascii')
        media_type = 'image/png' if image_path.suffix == '.png' else 'image/jpeg'
    from . import llm
    prompt = str(question)[:1000] or 'Describe the current desktop.'
    dimensions = _x11_display_size()
    if dimensions: prompt += f" The screenshot is {dimensions['width']} by {dimensions['height']} pixels. When locating a visible control, give its approximate center coordinates in that pixel space only if you can identify it confidently."
    answer = llm.describe_image(prompt, image)
    result = {'answer': answer, 'model': config.VISION_MODEL, 'captured': True, 'stored': False, 'screen_size': dimensions}
    if include_preview: result['preview'] = f'data:{media_type};base64,{image}'
    return result


def system_health():
    """Return actionable, non-secret diagnostics for Sage's host integrations."""
    linux = __import__('sys').platform.startswith('linux')
    screenshot = shutil.which('grim' if os.environ.get('WAYLAND_DISPLAY') else 'scrot')
    service = shutil.which('systemctl')
    user_manager = False
    if linux and service:
        try:
            r = subprocess.run([service, '--user', 'is-system-running'], capture_output=True, text=True, timeout=2)
            user_manager = r.returncode in (0, 1) and (r.stdout.strip() in ('running', 'degraded'))
        except Exception:
            pass
    return {
        'platform': __import__('sys').platform,
        'user': 'normal' if not hasattr(os, 'geteuid') or os.geteuid() != 0 else 'root (unsupported)',
        'session': 'Wayland' if os.environ.get('WAYLAND_DISPLAY') else ('X11' if os.environ.get('DISPLAY') else 'no desktop session'),
        'screenshot': bool(screenshot), 'screenshot_utility': Path(screenshot).name if screenshot else None,
        'desktop_input': bool(shutil.which('xdotool') and os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY')),
        'apt': bool(shutil.which('apt-get') and shutil.which('pkexec')),
        'systemd_user_manager': user_manager,
        'systemd_run': bool(shutil.which('systemd-run')),
        'notes': [
            'Sage source files are protected for ordinary shell commands; installs and source edits use the approval queue.',
            'Desktop screenshots are analyzed locally and discarded after the response.',
        ],
    }


def open_url(arguments):
    _linux_user()
    value = str(arguments.get('url', '')).strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Open only a complete http or https URL without embedded credentials.')
    opener = shutil.which('xdg-open')
    if not opener:
        raise RuntimeError('xdg-open is not installed on the Kali host.')
    subprocess.Popen([opener, value], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return {'opened': True, 'host': parsed.hostname}


def home_file_action(arguments, approved=False):
    """Perform bounded file operations inside the normal user's home directory."""
    _linux_user()
    home = Path.home().resolve()
    action = str(arguments.get('action', 'list'))
    raw_path = str(arguments.get('path', '.'))
    target = (home / raw_path).resolve()
    try:
        target.relative_to(home)
    except ValueError:
        raise PermissionError('File actions are limited to your home directory.')
    touches_source = is_sage_source_path(target)
    if touches_source and action in {'write','mkdir','copy','move','delete'} and not approved:
        raise PermissionError('Changing Sage source files requires approval.')
    if action == 'list':
        if not target.is_dir(): raise ValueError('Choose an existing folder.')
        return {'path': str(target.relative_to(home)), 'entries': [
            {'name': p.name, 'directory': p.is_dir(), 'size': p.stat().st_size if p.is_file() else None}
            for p in sorted(target.iterdir(), key=lambda p: p.name.lower())[:200]
        ]}
    if action == 'read':
        if not target.is_file(): raise ValueError('File does not exist.')
        if target.stat().st_size > 200_000: raise ValueError('Files larger than 200 KB are not read through this tool.')
        return {'path': str(target.relative_to(home)), 'content': target.read_text(encoding='utf-8', errors='replace')}
    if action == 'open':
        if not target.is_file(): raise ValueError('File does not exist.')
        if target.suffix.lower() in {'.deb','.rpm','.appimage','.msi','.exe','.run','.desktop'} or os.access(target, os.X_OK):
            raise PermissionError('Sage will not open executable or installer files through the file tool. Use the approved installation flow.')
        opener = shutil.which('xdg-open')
        if not opener: raise RuntimeError('xdg-open is not installed on the Kali host.')
        subprocess.Popen([opener, str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return {'opened': str(target.relative_to(home))}
    if action == 'write':
        content = str(arguments.get('content', ''))
        if len(content.encode('utf-8')) > 500_000: raise ValueError('Text is limited to 500 KB.')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return {'written': str(target.relative_to(home)), 'bytes': target.stat().st_size}
    other = (home / str(arguments.get('destination', ''))).resolve()
    try: other.relative_to(home)
    except ValueError: raise PermissionError('File actions are limited to your home directory.')
    if action in {'copy','move'} and (is_sage_source_path(other) or touches_source) and not approved:
        raise PermissionError('Changing Sage source files requires approval.')
    if action == 'mkdir':
        target.mkdir(parents=True, exist_ok=True); return {'created': str(target.relative_to(home))}
    if action == 'copy':
        if not target.is_file() or target.stat().st_size > 50_000_000: raise ValueError('Choose a file smaller than 50 MB.')
        other.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(target, other); return {'copied': str(target.relative_to(home)), 'to': str(other.relative_to(home))}
    if action == 'move':
        other.parent.mkdir(parents=True, exist_ok=True); shutil.move(str(target), str(other)); return {'moved': str(target.relative_to(home)), 'to': str(other.relative_to(home))}
    if action == 'delete':
        if target.is_dir(): raise ValueError('This tool does not recursively delete folders.')
        target.unlink(missing_ok=True); return {'deleted': str(target.relative_to(home))}
    raise ValueError('Choose list, read, write, mkdir, copy, move, or delete.')


def install_system_packages(arguments):
    _linux_user()
    packages = arguments.get('packages')
    if not isinstance(packages, list) or not 1 <= len(packages) <= 20:
        raise ValueError('Choose between 1 and 20 package names.')
    clean = []
    for package in packages:
        name = str(package).strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,127}', name):
            raise ValueError(f'Invalid APT package name: {package!r}')
        if name not in clean:
            clean.append(name)
    apt = shutil.which('apt-get')
    pkexec = shutil.which('pkexec')
    if not apt or not pkexec:
        raise RuntimeError('APT and pkexec are required. Install packages from Kali’s official repositories or use the Kali desktop package manager.')
    # pkexec invokes the active Polkit agent; Sage never collects or stores an administrator password.
    return {'packages': clean, 'source': 'configured APT repositories',
            **_run([pkexec, apt, 'install', '-y', '--', *clean], timeout=900)}


def desktop_action(arguments):
    _linux_user()
    if os.environ.get('WAYLAND_DISPLAY'):
        raise RuntimeError('This desktop action adapter currently supports X11. Wayland input injection needs a separate, explicitly configured desktop bridge.')
    if not os.environ.get('DISPLAY'):
        raise RuntimeError('No graphical desktop is attached to the Sage service. Start Sage from the Kali desktop session or import that session’s DISPLAY and XAUTHORITY into its user service.')
    xdotool = shutil.which('xdotool')
    if not xdotool:
        raise RuntimeError('xdotool is not installed. Ask Sage to install the xdotool package first.')
    # Sage never clicks through a visible system-package installer or privilege dialog.
    try:
        active = subprocess.run([xdotool, 'getactivewindow'], capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        title = subprocess.run([xdotool, 'getwindowname', active], capture_output=True, text=True, timeout=2, check=False).stdout
        pid = subprocess.run([xdotool, 'getwindowpid', active], capture_output=True, text=True, timeout=2, check=False).stdout.strip()
        cmdline = Path('/proc', pid, 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace') if pid.isdigit() else ''
        if re.search(r'(?i)(packagekit|synaptic|gdebi|gnome-software|discover|aptitude|dpkg|calamares|installer|software install)', title + ' ' + cmdline):
            raise PermissionError('Sage will not operate a system installer or privilege dialog. Use Sage’s installation request and confirm it yourself.')
    except subprocess.TimeoutExpired:
        raise RuntimeError('Could not identify the active desktop window; no input was sent.')
    action = arguments.get('action')
    if action == 'click':
        x, y = arguments.get('x'), arguments.get('y')
        if any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 10000 for n in (x, y)):
            raise ValueError('Click coordinates must be integers from 0 to 10000.')
        size = _x11_display_size()
        if size and (x >= size['width'] or y >= size['height']):
            raise ValueError(f"Click coordinates must fit the current {size['width']}×{size['height']} screen.")
        command = [xdotool, 'mousemove', '--sync', str(x), str(y), 'click', '1']
    elif action == 'type_text':
        text = str(arguments.get('text', ''))
        if not text or len(text) > 2000 or '\x00' in text:
            raise ValueError('Text must contain 1–2000 characters and no null bytes.')
        command = [xdotool, 'type', '--clearmodifiers', '--delay', '1', '--', text]
    elif action == 'press_key':
        key = str(arguments.get('key', ''))
        if not re.fullmatch(r'[A-Za-z0-9_+.-]{1,40}', key):
            raise ValueError('Key must use a simple X11 key name or chord such as Return or ctrl+l.')
        command = [xdotool, 'key', '--clearmodifiers', key]
    elif action == 'scroll':
        direction = arguments.get('direction')
        clicks = arguments.get('clicks', 3)
        if direction not in ('up', 'down') or isinstance(clicks, bool) or not isinstance(clicks, int) or not 1 <= clicks <= 10:
            raise ValueError('Choose up/down and 1–10 scroll steps.')
        command = [xdotool, 'click', '--repeat', str(clicks), '4' if direction == 'up' else '5']
    else:
        raise ValueError('Choose click, type_text, press_key, or scroll.')
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=10, check=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or 'xdotool action failed').strip()[:500])
    return {'action': action, 'completed': True}
