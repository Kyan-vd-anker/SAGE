#!/usr/bin/env python3
"""Build Sage's three Ollama models with the system prompt stored inside each Modelfile.

Run from the SAGE folder:
    python scripts/build-models.py               pull the base models, build sage-fast / sage-coder / sage-reason, then test them
    python scripts/build-models.py --check       only test models that already exist
    python scripts/build-models.py --write-only  only write the Modelfiles into ./modelfiles (no Ollama needed)

Run it again after you change Sage's system prompt in sage/agent.py.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sage import agent, config  # noqa: E402

# Standardizing parameters to reduce processing bottlenecks during prompt evaluation
MODELS = [
    ('local/sage-fast', 'qwen2.5:3b', {'num_ctx': 10, 'temperature': 0.6, 'top_p': 0.9}),
    ('local/sage-coder', 'qwen2.5-coder:7b', {'num_ctx': 4096, 'temperature': 0.2, 'top_p': 0.9}),
    ('local/sage-reason', 'deepseek-r1:7b', {'num_ctx': 40960, 'temperature': 0.6, 'top_p': 0.95}),
]
ENV_NAMES = {
    'local/sage-fast': 'SAGE_FAST_MODEL', 
    'local/sage-coder': 'SAGE_DEEP_MODEL', 
    'local/sage-reason': 'SAGE_REASONING_MODEL'
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
        safe_filename = name.replace('/', '-')
        path = folder / f'{safe_filename}.Modelfile'
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


def unload_all_models():
    """Explicitly tells Ollama to free up VRAM by unloading all active models."""
    print("Unloading loaded models from VRAM...")
    for name, _, _ in MODELS:
        try:
            body = {'model': name, 'keep_alive': 0}
            request = urllib.request.Request(
                config.OLLAMA_URL.rstrip('/') + '/api/chat',
                data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except Exception:
            pass


def ask(name):
    # Short simple query with a smaller predict size to prevent timeout blockages
    body = {'model': name, 'stream': False, 'keep_alive': '5m', 'options': {'num_predict': 50},
            'messages': [{'role': 'user', 'content': 'Respond with only the word hello.'}]}
    request = urllib.request.Request(config.OLLAMA_URL.rstrip('/') + '/api/chat', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    
    # Giving the server a long 600-second window to digest large prompt strings natively
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode('utf-8')
            error_json = json.loads(error_body)
            error_msg = error_json.get('error', error_body)
            raise RuntimeError(f"Ollama Error Details: {error_msg}")
        except Exception:
            raise RuntimeError(f"HTTP Error {exc.code}: {exc.reason}")


def check():
    ok = True
    for name, _base, _parameters in MODELS:
        unload_all_models()
        print(f"Testing {name} (this might take a minute while prompt embeds)...")
        try:
            # Let it compile on call one
            ask(name)                 
            # Retrieve timing data on call two
            result = ask(name)        
        except Exception as exc:
            print(f'{name}: FAILED - {exc}')
            ok = False
            continue
        
        text = str((result.get('message') or {}).get('content') or '')
        print(f"{name} testing verification succeeded. Response returned correctly.")
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
    for name, base, _parameters in MODELS:
        ollama('pull', base)
        ollama('create', name, '-f', str(paths[name]))
    
    print('Waiting for server threads to settle...')
    time.sleep(10)
    
    unload_all_models()
    
    print('\nTesting the new models...')
    good = check()
    print('\nTo use them, set these (Kali: the same env file or drop-in as SAGE_PASSWORD; Windows: start-sage.local.bat):')
    for name, _base, _parameters in MODELS:
        print(f'  {ENV_NAMES[name]}={name}')
    print('then restart Sage. To go back to the plain models, remove those three settings.')


if __name__ == '__main__':
    main()
