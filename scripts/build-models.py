#!/usr/bin/env python3
"""Build Sage's three Ollama models with the system prompt stored inside each Modelfile.

Run from the SAGE folder:
    python scripts/build-models.py               pull the base models, build sage-fast / sage-coder / sage-reason, then test them
    python scripts/build-models.py --check       only test models that already exist
    python scripts/build-models.py --write-only  only write the Modelfiles into ./modelfiles (no Ollama needed)

Run it again after you change Sage's system prompt in sage/agent.py. Sage compares each model's stored prompt with the current one
(through Ollama's /api/show) and falls back to sending the full prompt when they differ, so a model you forgot to rebuild is slower
but never wrong; --check tells you which ones are out of date.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sage import agent, config  # noqa: E402

# Same context size everywhere: Sage's document reading also asks for 32000, and a request with a different num_ctx makes Ollama reload
# the model and throw its prompt cache away.
MODELS = [
    ('local/sage-fast', 'qwen3.5:4b', {'num_ctx': 32000, 'temperature': 0.6, 'top_p': 0.9}),
    ('local/sage-coder', 'qwen3.5:9b', {'num_ctx': 32000, 'temperature': 0.2, 'top_p': 0.9}),
    ('local/sage-reason', 'qwen3.5:9b', {'num_ctx': 32000, 'temperature': 0.6, 'top_p': 0.95}),
]
ENV_NAMES = {
    'local/sage-fast': 'SAGE_FAST_MODEL',
    'local/sage-coder': 'SAGE_DEEP_MODEL',
    'local/sage-reason': 'SAGE_REASONING_MODEL',
}


def modelfile_text(base, parameters):
    prompt = agent.SYSTEM
    if '"""' in prompt:
        raise SystemExit('The system prompt contains triple quotes, which a Modelfile cannot hold.')
    lines = [f'FROM {base}']
    for key, value in parameters.items():
        lines.append(f'PARAMETER {key} {value}')
    lines.append(f'SYSTEM """{prompt}"""')
    return '\n'.join(lines) + '\n'


def write_modelfiles():
    folder = ROOT / 'modelfiles'
    folder.mkdir(exist_ok=True)
    paths = {}
    for name, base, parameters in MODELS:
        path = folder / f"{name.replace('/', '-')}.Modelfile"
        path.write_text(modelfile_text(base, parameters), encoding='utf-8')
        paths[name] = path
    return paths


def ollama(*args):
    print('> ollama', *args)
    try:
        subprocess.run(['ollama', *args], check=True)
    except FileNotFoundError:
        raise SystemExit('The ollama command was not found. Install Ollama and make sure it is on PATH.')
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f'ollama {args} failed (exit code {exc.returncode}).')


def post(path, body, timeout):
    request = urllib.request.Request(config.OLLAMA_URL.rstrip('/') + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode('utf-8')).get('error')
        except Exception:
            detail = None
        raise RuntimeError(f'Ollama error {exc.code}: {detail or exc.reason}') from None


def unload_all_models():
    """Tell Ollama to free the VRAM of the models so each one is tested from a cold start."""
    print('Unloading loaded models from VRAM...')
    for name, _, _ in MODELS:
        try:
            post('/api/chat', {'model': name, 'keep_alive': 0}, 5)
        except Exception:
            pass


def ask(name):
    """One short test question. Thinking is switched off so a model that normally reasons first still answers within the few tokens
    allowed here; a server or model that does not know the option is asked again without it."""
    body = {'model': name, 'stream': False, 'keep_alive': '5m', 'think': False, 'options': {'num_predict': 60},
            'messages': [{'role': 'user', 'content': 'Respond with only the word hello.'}]}
    try:
        return post('/api/chat', body, 600)
    except RuntimeError as exc:
        if 'think' not in str(exc).lower():
            raise
        body.pop('think')
        return post('/api/chat', body, 600)


def stored_prompt_state(name):
    """'current' | 'outdated' | 'unknown': is the system prompt stored in the model the one Sage uses now?"""
    try:
        info = post('/api/show', {'model': name}, 20)
    except Exception:
        return 'unknown'
    stored = info.get('system') if isinstance(info, dict) else None
    if not isinstance(stored, str) or not stored.strip():
        match = re.search(r'SYSTEM\s+"""(.*?)"""', str((info or {}).get('modelfile') or ''), re.S)
        stored = match.group(1) if match else None
    if stored is None:
        return 'unknown'
    return 'current' if stored.strip() == agent.SYSTEM.strip() else 'outdated'


def check():
    ok = True
    for name, _base, _parameters in MODELS:
        unload_all_models()
        print(f'Testing {name} (the first call loads the model, so this can take a minute)...')
        try:
            ask(name)                 # call one loads the model
            result = ask(name)        # call two shows the warm speed
        except Exception as exc:
            print(f'{name}: FAILED - {exc}')
            ok = False
            continue
        reply = str((result.get('message') or {}).get('content') or '').strip()
        if not reply:
            thinking = str((result.get('message') or {}).get('thinking') or '').strip()
            hint = ' It only produced hidden reasoning; this Ollama or model ignores think=false.' if thinking else ''
            print(f'{name}: FAILED - the model returned an empty reply.{hint}')
            ok = False
            continue
        seconds = float(result.get('eval_duration') or 0) / 1e9
        speed = f"{int(result.get('eval_count') or 0) / seconds:.1f} tokens/s" if seconds > 0 else 'speed unknown'
        state = stored_prompt_state(name)
        print(f'{name}: OK - replied "{reply[:40]}" ({speed}); stored system prompt: {state}')
        if state == 'outdated':
            print(f'  {name} holds an older Sage prompt. Run this script without --check to rebuild it.')
            ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='only test existing models')
    parser.add_argument('--write-only', action='store_true', help='only write the Modelfiles')
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check() else 1)
    paths = write_modelfiles()
    print(f'Wrote {len(paths)} Modelfiles to {ROOT / "modelfiles"}')
    if args.write_only:
        return
    for base in dict.fromkeys(base for _name, base, _parameters in MODELS):      # each base model is downloaded once
        ollama('pull', base)
    for name, _base, _parameters in MODELS:
        ollama('create', name, '-f', str(paths[name]))

    print('Waiting for Ollama to settle...')
    time.sleep(10)
    unload_all_models()

    print('\nTesting the new models...')
    good = check()
    print('\nTo use them, set these (Kali: the same env file or drop-in as SAGE_PASSWORD; Windows: start-sage.local.bat):')
    for name, _base, _parameters in MODELS:
        print(f'  {ENV_NAMES[name]}={name}')
    print('then restart Sage. To go back to the plain models, remove those three settings.')
    if not good:
        print('\nOne or more models failed the test (see above). Fix that before using them.')
        sys.exit(1)


if __name__ == '__main__':
    main()
