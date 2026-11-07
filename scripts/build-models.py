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
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sage import agent, config  # noqa: E402

# name, base model, Modelfile parameters
MODELS = [
    ('sage-fast', 'qwen2.5:3b', {'num_ctx': 8192, 'temperature': 0.6, 'top_p': 0.9, 'repeat_penalty': 1.05}),
    ('sage-coder', 'qwen2.5-coder:7b', {'num_ctx': 8192, 'temperature': 0.2, 'top_p': 0.9}),
    ('sage-reason', 'deepseek-r1:7b', {'num_ctx': 8192, 'temperature': 0.6, 'top_p': 0.95}),
]
ENV_NAMES = {'sage-fast': 'SAGE_FAST_MODEL', 'sage-coder': 'SAGE_DEEP_MODEL', 'sage-reason': 'SAGE_REASONING_MODEL'}


def modelfile_text(base, parameters):
    prompt = agent.SYSTEM
    if '"""' in prompt:
        raise SystemExit('The system prompt contains triple quotes, which a Modelfile cannot hold.')
    lines = [f'FROM {base}']
    lines += [f'PARAMETER {key} {value}' for key, value in parameters.items()]
    lines.append(f'SYSTEM """{prompt}"""')
    return '\n'.join(lines) + '\n'


def write_modelfiles():
    folder = ROOT / 'modelfiles'
    folder.mkdir(exist_ok=True)
    paths = {}
    for name, base, parameters in MODELS:
        path = folder / f'{name}.Modelfile'
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
        raise SystemExit(f'ollama {args[0]} failed (exit code {exc.returncode}).')


def ask(name):
    body = {'model': name, 'stream': False, 'keep_alive': '30m', 'options': {'num_predict': 200},
            'messages': [{'role': 'user', 'content': 'Who are you? Answer in one short sentence.'}]}
    request = urllib.request.Request(config.OLLAMA_URL.rstrip('/') + '/api/chat', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())


def check():
    ok = True
    for name, _base, _parameters in MODELS:
        try:
            ask(name)                 # first call loads the model
            result = ask(name)        # second call shows the warm timings
        except Exception as exc:
            print(f'{name}: FAILED - {exc}')
            ok = False
            continue
        text = str((result.get('message') or {}).get('content') or '')
        knows_prompt = 'sage' in text.lower()
        print(f"{name}: stored prompt {'applied' if knows_prompt else 'NOT applied'}; "
              f"prompt tokens {result.get('prompt_eval_count')}, prompt time {round(result.get('prompt_eval_duration', 0) / 1e6)} ms, "
              f"load time {round(result.get('load_duration', 0) / 1e6)} ms")
        ok = ok and knows_prompt
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
    time.sleep(5)  # give Ollama a moment to finish creating the models
    print('\nTesting the new models...')
    good = check()
    print('\nTo use them, set these (Kali: the same env file or drop-in as SAGE_PASSWORD; Windows: start-sage.local.bat):')
    for name, _base, _parameters in MODELS:
        print(f'  {ENV_NAMES[name]}={name}')
    print('then restart Sage. To go back to the plain models, remove those three settings.')
    if not good:
        raise SystemExit('At least one model did not show the stored prompt. Do not switch Sage to the sage-* models yet.')


if __name__ == '__main__':
    main()
