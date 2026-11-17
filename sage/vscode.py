"""Endpoints for VS Code.

An OpenAI-compatible API, so extensions such as Continue, Cline or any "OpenAI-compatible" provider can use Sage's models:

    GET  /v1/models              the three lanes: sage-fast, sage-coder, sage-reason
    POST /v1/chat/completions    chat, streaming (SSE) or not, with tool calls; the reasoning lane returns `reasoning_content`
    POST /v1/completions         inline completion: `prompt` (before the cursor) and `suffix` (after it); fast model
    POST /v1/embeddings          for codebase indexing
    POST /api/vscode/inline      the same inline completion with editor-friendly fields (prefix, suffix, language, path)
    GET  /api/vscode/status      health and configuration

Everything needs `Authorization: Bearer <SAGE_VSCODE_TOKEN>`. The lanes run on the models Sage is configured with:

    sage-fast    SAGE_FAST_MODEL        inline suggestions, thinking off
    sage-coder   SAGE_DEEP_MODEL        code chat, thinking off
    sage-reason  SAGE_REASONING_MODEL   hard problems, thinking on

Only these configured models are reachable: an extension cannot ask Ollama for any other model through Sage.

Inline completions use the model's own fill-in-the-middle support when Ollama reports it (capability "insert"); otherwise a short
chat prompt with a <CURSOR> marker is used and the answer is cleaned up (fences, repeated code, line limits). A newer request from
the same editor cancels the one still running, because the earlier one is stale as soon as another character is typed.
"""
import http.client
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from urllib.parse import urlparse

from . import config

LANES = ('sage-fast', 'sage-coder', 'sage-reason')
INLINE_SYSTEM = ('You are a code completion engine inside a code editor. The cursor is marked <CURSOR>. Reply with ONLY the text to insert at the '
                 'cursor: no explanation, no markdown fences, and do not repeat the code before or after the cursor. Match the style and indentation '
                 'of the file. If nothing should be inserted, reply with nothing.')
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_INLINE = {}
_INLINE_LOCK = threading.Lock()
_CAPABILITIES = {}              # model -> (checked at, set of capabilities or None)
_NO_INSERT = set()              # models whose fill-in-the-middle request was refused
_SLOTS = threading.BoundedSemaphore(max(1, config.VSCODE_MAX_CONCURRENT))


class ApiError(Exception):
    def __init__(self, status, message, code='invalid_request_error'):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


class UpstreamError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def error_body(message, code='invalid_request_error'):
    return {'error': {'message': message, 'type': code, 'code': code}}


# ------------------------------------------------------------------ lanes

def lanes():
    return {'sage-fast': {'model': config.FAST_MODEL, 'think': config.FAST_THINK},
            'sage-coder': {'model': config.DEEP_MODEL, 'think': config.DEEP_THINK},
            'sage-reason': {'model': config.REASONING_MODEL, 'think': config.REASONING_THINK}}


def resolve(requested, default):
    """-> (lane id, Ollama model, think). Unknown ids (extensions often send their own) fall back to `default`."""
    table = lanes()
    name = str(requested or '').strip()
    if name == 'sage':
        name = 'sage-coder'
    lane = name if name in table else next((k for k, v in table.items() if v['model'] == name), None) or default
    return lane, table[lane]['model'], table[lane]['think']


# ------------------------------------------------------------------ Ollama transport

def _connect(timeout):
    url = urlparse(config.OLLAMA_URL)
    return http.client.HTTPConnection(url.hostname or '127.0.0.1', url.port or 11434, timeout=timeout)


def _read_error(response):
    raw = b''
    try:
        raw = response.read()
        message = json.loads(raw.decode('utf-8', 'replace')).get('error')
    except Exception:
        message = None
    return message or raw.decode('utf-8', 'replace')[:300] or response.reason


def _stream(path, body, cancel=None, timeout=600):
    """Decoded NDJSON objects from Ollama. Closing the generator closes the connection, which makes Ollama stop generating."""
    conn = _connect(timeout)
    try:
        conn.request('POST', path, body=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise UpstreamError(response.status, _read_error(response))
        for line in response:
            if cancel is not None and cancel.is_set():
                return
            line = line.strip()
            if line:
                yield json.loads(line)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def events(path, body, cancel=None):
    """Like _stream, but a server or model that does not know the `think` option is asked again without it."""
    try:
        yield from _stream(path, body, cancel)
    except UpstreamError as exc:
        if exc.status == 400 and 'think' in body and 'think' in str(exc).lower():
            yield from _stream(path, {k: v for k, v in body.items() if k != 'think'}, cancel)
        else:
            raise


def _post_json(path, body, timeout=120):
    conn = _connect(timeout)
    try:
        conn.request('POST', path, body=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise UpstreamError(response.status, _read_error(response))
        return json.loads(response.read())
    finally:
        conn.close()


def _capabilities(model):
    """Ollama's capability list for a model (cached for ten minutes); None when it cannot be read."""
    now = time.monotonic()
    cached = _CAPABILITIES.get(model)
    if cached and now - cached[0] < 600:
        return cached[1]
    try:
        info = _post_json('/api/show', {'model': model}, timeout=5)
        found = set(info.get('capabilities') or []) or None
    except Exception:
        found = None
    _CAPABILITIES[model] = (now, found)
    return found


def _upstream_failure(exc):
    if isinstance(exc, UpstreamError):
        return ApiError(502 if exc.status >= 500 else 400 if exc.status == 400 else 502, f'Ollama: {exc}', 'upstream_error')
    return ApiError(503, f'Ollama is not reachable: {exc}', 'upstream_unavailable')


def _acquire_slot():
    if not _SLOTS.acquire(blocking=False):
        raise ApiError(429, 'Too many requests are running at once. Try again in a moment.', 'rate_limit_exceeded')


class _Guarded:
    """Wraps a streaming response so the request slot is released exactly once: when the stream ends, fails, or is closed by the server
    because the editor went away, even if it never started."""
    def __init__(self, generator):
        self._generator, self._released = generator, False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._generator)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            self._generator.close()
        finally:
            if not self._released:
                self._released = True
                _SLOTS.release()


# ------------------------------------------------------------------ chat

def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict) and part.get('type') in (None, 'text', 'input_text'))
    return '' if content is None else str(content)


def to_ollama_messages(messages):
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise ApiError(400, '`messages` must be a non-empty list of messages')
    out = []
    for message in messages:
        role = {'developer': 'system'}.get(message.get('role'), message.get('role'))
        role = role if role in ('system', 'user', 'assistant', 'tool') else 'user'
        item = {'role': role, 'content': _text(message.get('content'))}
        if role == 'assistant' and message.get('tool_calls'):
            calls = []
            for call in message['tool_calls']:
                function = (call or {}).get('function') or {}
                arguments = function.get('arguments')
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or '{}')
                    except ValueError:
                        arguments = {}
                calls.append({'function': {'name': function.get('name', ''), 'arguments': arguments if isinstance(arguments, dict) else {}}})
            item['tool_calls'] = calls
        if role == 'tool' and message.get('name'):
            item['tool_name'] = message['name']
        out.append(item)
    return out


def _openai_tool_calls(calls, with_index=False):
    out = []
    for number, call in enumerate(calls or []):
        function = (call or {}).get('function') or {}
        arguments = function.get('arguments')
        entry = {'id': f'call_{uuid.uuid4().hex[:24]}', 'type': 'function',
                 'function': {'name': function.get('name', ''), 'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments or {})}}
        if with_index:
            entry['index'] = number
        out.append(entry)
    return out


def _estimate_tokens(messages, tools):
    return int((sum(len(str(m.get('content') or '')) for m in messages) + len(json.dumps(tools or []))) / 3.0) + 8 * len(messages)


def _chat_request(body):
    lane, model, think = resolve(body.get('model'), 'sage-coder')
    messages = to_ollama_messages(body.get('messages'))
    tools = body.get('tools') if isinstance(body.get('tools'), list) and body.get('tools') else None
    num_ctx = config.VSCODE_NUM_CTX
    need = _estimate_tokens(messages, tools)
    if need > num_ctx - 256:
        # Ollama would silently drop the start of the prompt. Say so instead, so the extension can send less.
        raise ApiError(400, f'This request needs about {need} tokens of context but {lane} has a window of {num_ctx}. Send less code or history.', 'context_length_exceeded')
    options = {'num_ctx': num_ctx}
    for source, target in (('temperature', 'temperature'), ('top_p', 'top_p'), ('seed', 'seed')):
        if body.get(source) is not None:
            options[target] = body[source]
    limit = body.get('max_completion_tokens') or body.get('max_tokens')
    try:
        limit = int(limit) if limit else (4096 if think else 2048)
    except (TypeError, ValueError):
        limit = 2048
    options['num_predict'] = max(limit, 2048) if think else max(1, limit)      # thinking tokens count: leave room for the answer
    stop = body.get('stop')
    if stop:
        options['stop'] = [stop] if isinstance(stop, str) else [str(s) for s in stop][:4]
    upstream = {'model': model, 'messages': messages, 'stream': True, 'keep_alive': config.OLLAMA_KEEP_ALIVE, 'think': think, 'options': options}
    if tools:
        upstream['tools'] = tools
    return lane, upstream


def _finish_reason(done_reason, tool_calls):
    if tool_calls:
        return 'tool_calls'
    return 'length' if done_reason == 'length' else 'stop'


def chat(body):
    lane, upstream = _chat_request(body)
    _acquire_slot()
    try:
        content, thinking, calls, last = [], [], [], {}
        try:
            for event in events('/api/chat', upstream):
                message = event.get('message') or {}
                if message.get('content'):
                    content.append(message['content'])
                if message.get('thinking'):
                    thinking.append(message['thinking'])
                calls.extend(message.get('tool_calls') or [])
                if event.get('done'):
                    last = event
        except Exception as exc:
            raise _upstream_failure(exc)
    finally:
        _SLOTS.release()
    reply = {'role': 'assistant', 'content': ''.join(content) or None}
    if thinking:
        reply['reasoning_content'] = ''.join(thinking)
    tool_calls = _openai_tool_calls(calls)
    if tool_calls:
        reply['tool_calls'] = tool_calls
    prompt, completion = int(last.get('prompt_eval_count') or 0), int(last.get('eval_count') or 0)
    return ('json', 200, {'id': f'chatcmpl-{uuid.uuid4().hex[:24]}', 'object': 'chat.completion', 'created': int(time.time()), 'model': lane,
                          'choices': [{'index': 0, 'message': reply, 'finish_reason': _finish_reason(last.get('done_reason'), tool_calls)}],
                          'usage': {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion}})


def _sse(value):
    return 'data: ' + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) + '\n\n'


def chat_stream(body):
    lane, upstream = _chat_request(body)
    _acquire_slot()
    identity = f'chatcmpl-{uuid.uuid4().hex[:24]}'
    created = int(time.time())
    include_usage = bool((body.get('stream_options') or {}).get('include_usage'))

    def chunk(delta, finish=None, usage=None):
        value = {'id': identity, 'object': 'chat.completion.chunk', 'created': created, 'model': lane,
                 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if usage is not None:
            value['usage'] = usage
        return _sse(value)

    def generate():
        source = events('/api/chat', upstream)
        try:
            yield chunk({'role': 'assistant', 'content': ''})
            saw_calls, last = False, {}
            try:
                for event in source:
                    message = event.get('message') or {}
                    if message.get('thinking'):
                        yield chunk({'reasoning_content': message['thinking']})
                    if message.get('content'):
                        yield chunk({'content': message['content']})
                    if message.get('tool_calls'):
                        saw_calls = True
                        yield chunk({'tool_calls': _openai_tool_calls(message['tool_calls'], with_index=True)})
                    if event.get('done'):
                        last = event
            except Exception as exc:
                failure = _upstream_failure(exc)
                yield _sse(error_body(failure.message, failure.code))
                yield _sse('[DONE]')
                return
            yield chunk({}, _finish_reason(last.get('done_reason'), saw_calls))
            if include_usage:
                prompt, completion = int(last.get('prompt_eval_count') or 0), int(last.get('eval_count') or 0)
                yield _sse({'id': identity, 'object': 'chat.completion.chunk', 'created': created, 'model': lane, 'choices': [],
                            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion}})
            yield _sse('[DONE]')
        finally:
            source.close()                                   # the editor went away or the answer is finished: stop Ollama
    return ('sse', _Guarded(generate()))


# ------------------------------------------------------------------ inline completion

def _supports_insert(model):
    if model in _NO_INSERT:
        return False
    mode = config.VSCODE_FIM
    if mode == 'off':
        return False
    if mode == 'on':
        return True
    found = _capabilities(model)
    return bool(found and 'insert' in found)


def _register(client):
    """A newer inline request from the same editor makes the one still running stale: cancel it."""
    event = threading.Event()
    with _INLINE_LOCK:
        previous = _INLINE.get(client)
        if previous is not None:
            previous.set()
        _INLINE[client] = event
    return event


def _unregister(client, event):
    with _INLINE_LOCK:
        if _INLINE.get(client) is event:
            _INLINE.pop(client, None)


def clean_completion(text, prefix, suffix, single_line, max_lines=12):
    """Make a model's answer safe to insert: no fences or markers, no repeat of the code before or after the cursor, line limits."""
    text = str(text or '').replace('<CURSOR>', '').replace('\r\n', '\n')
    text = re.sub(r'^\s*```[\w+-]*[ \t]*\n', '', text)
    text = re.sub(r'\n?```\s*$', '', text)
    last_line = prefix.rsplit('\n', 1)[-1]
    if last_line.strip() and text.startswith(last_line):         # the model repeated the line it was asked to continue
        text = text[len(last_line):]
    head = suffix.lstrip(' \t')[:200]
    for size in range(min(len(text), len(head)), 2, -1):        # ...or the code that already follows the cursor
        if head[:size].strip() and text.endswith(head[:size]):
            text = text[:-size]
            break
    if single_line:
        text = text.split('\n', 1)[0]
    else:
        text = '\n'.join(text.split('\n')[:max_lines])
    return text.rstrip() if text.strip() else ''


def _cache_get(key):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < 120:
            _CACHE.move_to_end(key)
            return hit[1]
        _CACHE.pop(key, None)
    return None


def _cache_put(key, value):
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), value)
        while len(_CACHE) > 128:
            _CACHE.popitem(last=False)


def inline_complete(prefix, suffix='', language='', path='', max_tokens=128, temperature=0.2, stop=None, single_line=None, client='editor'):
    """The text to insert at the cursor. -> {'text', 'model', 'method': 'native'|'chat'|'none', 'cached', 'superseded'}"""
    model = config.FAST_MODEL
    prefix = str(prefix or '')[-config.VSCODE_PREFIX_CHARS:]
    suffix = str(suffix or '')[:config.VSCODE_SUFFIX_CHARS]
    if not prefix.strip() and not suffix.strip():
        return {'text': '', 'model': 'sage-fast', 'method': 'none', 'cached': False, 'superseded': False}
    if single_line is None:
        single_line = bool(suffix.split('\n', 1)[0].strip())      # text after the cursor on this line: only finish the line
    try:
        max_tokens = max(1, min(int(max_tokens or 128), 512))
        temperature = float(temperature if temperature is not None else 0.2)
    except (TypeError, ValueError):
        raise ApiError(400, 'max_tokens and temperature must be numbers')
    stops = [str(s) for s in ([stop] if isinstance(stop, str) else stop or [])][:4]
    key = (model, prefix[-2500:], suffix[:600], language, max_tokens, round(temperature, 2), tuple(stops), single_line)
    cached = _cache_get(key)
    if cached is not None:
        return {'text': cached, 'model': 'sage-fast', 'method': 'cache', 'cached': True, 'superseded': False}
    _acquire_slot()
    cancel = _register(client)
    try:
        method, raw = None, ''
        options = {'num_predict': max_tokens, 'temperature': temperature, 'top_p': 0.9, 'num_ctx': config.VSCODE_NUM_CTX}
        if stops:
            options['stop'] = stops
        if _supports_insert(model):
            body = {'model': model, 'prompt': prefix, 'suffix': suffix, 'stream': True, 'keep_alive': config.OLLAMA_KEEP_ALIVE, 'think': False, 'options': options}
            try:
                parts = []
                for event in events('/api/generate', body, cancel):
                    parts.append(event.get('response') or '')
                    if single_line and '\n' in ''.join(parts):
                        break
                raw, method = ''.join(parts), 'native'
            except UpstreamError as exc:
                if 'insert' in str(exc).lower() or 'suffix' in str(exc).lower():
                    _NO_INSERT.add(model)                        # this model has no fill-in-the-middle template: use the chat prompt
                else:
                    raise _upstream_failure(exc)
            except Exception as exc:
                raise _upstream_failure(exc)
        if method is None and not cancel.is_set():
            where = f'File: {path or "untitled"}' + (f' ({language})' if language else '')
            body = {'model': model, 'stream': True, 'keep_alive': config.OLLAMA_KEEP_ALIVE, 'think': False, 'options': options,
                    'messages': [{'role': 'system', 'content': INLINE_SYSTEM},
                                 {'role': 'user', 'content': f'{where}\n\n{prefix}<CURSOR>{suffix}'}]}
            try:
                parts = []
                for event in events('/api/chat', body, cancel):
                    parts.append((event.get('message') or {}).get('content') or '')
                    if single_line and '\n' in ''.join(parts).lstrip('\r'):
                        break
                raw, method = ''.join(parts), 'chat'
            except Exception as exc:
                raise _upstream_failure(exc)
        if cancel.is_set():
            return {'text': '', 'model': 'sage-fast', 'method': method or 'none', 'cached': False, 'superseded': True}
        text = clean_completion(raw, prefix, suffix, single_line)
        _cache_put(key, text)
        return {'text': text, 'model': 'sage-fast', 'method': method, 'cached': False, 'superseded': False}
    finally:
        _SLOTS.release()
        _unregister(client, cancel)


def completions(body, client):
    """OpenAI /v1/completions: `prompt` is the code before the cursor, `suffix` the code after it."""
    prompt = body.get('prompt')
    if isinstance(prompt, list):
        prompt = prompt[0] if prompt and isinstance(prompt[0], str) else ''
    if not isinstance(prompt, str):
        raise ApiError(400, '`prompt` must be a string')
    result = inline_complete(prompt, body.get('suffix') or '', max_tokens=body.get('max_tokens') or 128,
                             temperature=body.get('temperature'), stop=body.get('stop'), client=client)
    identity, created = f'cmpl-{uuid.uuid4().hex[:24]}', int(time.time())
    tokens = max(0, len(result['text']) // 3)
    usage = {'prompt_tokens': 0, 'completion_tokens': tokens, 'total_tokens': tokens}
    if body.get('stream'):
        def generate():
            yield _sse({'id': identity, 'object': 'text_completion', 'created': created, 'model': 'sage-fast',
                        'choices': [{'index': 0, 'text': result['text'], 'finish_reason': None}]})
            yield _sse({'id': identity, 'object': 'text_completion', 'created': created, 'model': 'sage-fast',
                        'choices': [{'index': 0, 'text': '', 'finish_reason': 'stop'}]})
            yield _sse('[DONE]')
        return ('sse', generate())
    return ('json', 200, {'id': identity, 'object': 'text_completion', 'created': created, 'model': 'sage-fast',
                          'choices': [{'index': 0, 'text': result['text'], 'finish_reason': 'stop'}], 'usage': usage})


def inline(body, client):
    start = time.monotonic()
    result = inline_complete(body.get('prefix') or '', body.get('suffix') or '', language=str(body.get('language') or '')[:40],
                             path=str(body.get('path') or '')[:300], max_tokens=body.get('max_tokens') or 128, temperature=body.get('temperature'),
                             stop=body.get('stop'), single_line=body.get('single_line'), client=client)
    result['completion'] = result.pop('text')
    result['ms'] = int((time.monotonic() - start) * 1000)
    return ('json', 200, result)


# ------------------------------------------------------------------ other endpoints

def embeddings(body):
    items = body.get('input')
    items = [items] if isinstance(items, str) else items
    if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items) or len(items) > 256:
        raise ApiError(400, '`input` must be a string or a list of up to 256 strings')
    try:
        data = _post_json('/api/embed', {'model': config.EMBED_MODEL, 'input': [i[:8000] for i in items], 'keep_alive': config.OLLAMA_KEEP_ALIVE}, timeout=120)
    except Exception as exc:
        raise _upstream_failure(exc)
    vectors = data.get('embeddings') or []
    return ('json', 200, {'object': 'list', 'model': config.EMBED_MODEL,
                          'data': [{'object': 'embedding', 'index': n, 'embedding': v} for n, v in enumerate(vectors)],
                          'usage': {'prompt_tokens': int(data.get('prompt_eval_count') or 0), 'total_tokens': int(data.get('prompt_eval_count') or 0)}})


def models():
    return ('json', 200, {'object': 'list', 'data': [{'id': lane, 'object': 'model', 'created': 0, 'owned_by': 'sage'} for lane in LANES]})


def status():
    table = lanes()
    fim = 'off' if config.VSCODE_FIM == 'off' else ('native' if _supports_insert(table['sage-fast']['model']) else 'chat')
    return ('json', 200, {'ok': True, 'version': config.VERSION, 'num_ctx': config.VSCODE_NUM_CTX,
                          'lanes': {lane: {'model': v['model'], 'think': v['think']} for lane, v in table.items()},
                          'inline': {'lane': 'sage-fast', 'method': fim, 'prefix_chars': config.VSCODE_PREFIX_CHARS, 'suffix_chars': config.VSCODE_SUFFIX_CHARS}})


def handle(method, path, body, client):
    """-> ('json', status, value) or ('sse', generator). Raises ApiError."""
    if method == 'GET':
        if path == '/v1/models':
            return models()
        if path == '/api/vscode/status':
            return status()
        raise ApiError(404, 'Not found', 'not_found')
    if path == '/v1/chat/completions':
        return chat_stream(body) if body.get('stream') else chat(body)
    if path == '/v1/completions':
        return completions(body, client)
    if path == '/v1/embeddings':
        return embeddings(body)
    if path == '/api/vscode/inline':
        return inline(body, client)
    raise ApiError(404, 'Not found', 'not_found')
