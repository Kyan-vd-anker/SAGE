import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from sage import config, db, server, vscode

TOKEN = 'test-token'


class FakeOllama:
    """Stands in for Ollama's streaming endpoints. `replies` maps a path to a list of events or a function(body) -> events."""
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, path, body, cancel=None, timeout=600):
        self.calls.append((path, body))
        reply = self.replies[path]
        items = reply(body) if callable(reply) else reply
        for item in items:
            if isinstance(item, Exception):
                raise item
            if cancel is not None and cancel.is_set():
                return
            yield item

    def bodies(self, path):
        return [b for p, b in self.calls if p == path]


def chat_events(text, thinking='', calls=None, done_reason='stop'):
    events = []
    if thinking:
        events.append({'message': {'role': 'assistant', 'thinking': thinking}, 'done': False})
    for piece in [text[i:i + 6] for i in range(0, len(text), 6)]:
        events.append({'message': {'role': 'assistant', 'content': piece}, 'done': False})
    if calls:
        events.append({'message': {'role': 'assistant', 'content': '', 'tool_calls': calls}, 'done': False})
    events.append({'message': {'role': 'assistant', 'content': ''}, 'done': True, 'done_reason': done_reason, 'prompt_eval_count': 42, 'eval_count': 7})
    return events


class Base(unittest.TestCase):
    def setUp(self):
        for target, value in (('VSCODE_TOKEN', TOKEN), ('VSCODE_FIM', 'auto'), ('VSCODE_NUM_CTX', 32000), ('FAST_MODEL', 'local/sage-fast'),
                              ('DEEP_MODEL', 'local/sage-coder'), ('REASONING_MODEL', 'local/sage-reason'), ('FAST_THINK', False),
                              ('DEEP_THINK', False), ('REASONING_THINK', True)):
            patcher = patch.object(config, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        vscode._CACHE.clear()
        vscode._CAPABILITIES.clear()
        vscode._NO_INSERT.clear()
        vscode._INLINE.clear()

    def fake(self, replies, capabilities=None):
        fake = FakeOllama(replies)
        for target, value in (('sage.vscode._stream', fake), ('sage.vscode._capabilities', lambda model: capabilities)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake


class LaneTests(Base):
    def test_the_three_lanes_are_listed(self):
        kind, status, body = vscode.handle('GET', '/v1/models', {}, 'c')
        self.assertEqual([m['id'] for m in body['data']], ['sage-fast', 'sage-coder', 'sage-reason'])

    def test_lanes_map_to_the_configured_models_and_unknown_ids_fall_back(self):
        self.assertEqual(vscode.resolve('sage-reason', 'sage-coder'), ('sage-reason', 'local/sage-reason', True))
        self.assertEqual(vscode.resolve('sage-fast', 'sage-coder'), ('sage-fast', 'local/sage-fast', False))
        self.assertEqual(vscode.resolve('local/sage-coder', 'sage-fast')[0], 'sage-coder')           # a configured Ollama name
        self.assertEqual(vscode.resolve('gpt-4o', 'sage-coder'), ('sage-coder', 'local/sage-coder', False))
        self.assertEqual(vscode.resolve('llama3:70b', 'sage-fast')[1], 'local/sage-fast')            # no other model is reachable through Sage

    def test_thinking_is_off_for_fast_and_coder_and_on_for_reasoning(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('ok')})
        for lane in ('sage-fast', 'sage-coder', 'sage-reason'):
            vscode.handle('POST', '/v1/chat/completions', {'model': lane, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        self.assertEqual([b['think'] for b in fake.bodies('/api/chat')], [False, False, True])
        self.assertEqual([b['model'] for b in fake.bodies('/api/chat')], ['local/sage-fast', 'local/sage-coder', 'local/sage-reason'])

    def test_a_model_that_does_not_know_the_think_option_is_asked_again_without_it(self):
        calls = []

        def transport(path, body, cancel=None, timeout=600):
            calls.append(dict(body))
            if 'think' in body:
                raise vscode.UpstreamError(400, '"local/sage-fast" does not support think')
            yield from chat_events('fine')
        with patch('sage.vscode._stream', transport):
            kind, status, body = vscode.handle('POST', '/v1/chat/completions', {'model': 'sage-fast', 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        self.assertEqual(body['choices'][0]['message']['content'], 'fine')
        self.assertEqual(['think' in c for c in calls], [True, False])


class ChatTests(Base):
    def chat(self, **extra):
        body = {'model': 'sage-coder', 'messages': [{'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'write a loop'}], **extra}
        return vscode.handle('POST', '/v1/chat/completions', body, 'c')

    def test_a_reply_has_the_openai_shape_and_usage(self):
        self.fake({'/api/chat': lambda body: chat_events('for i in range(3): print(i)')})
        kind, status, body = self.chat()
        self.assertEqual((kind, status, body['object'], body['model']), ('json', 200, 'chat.completion', 'sage-coder'))
        self.assertEqual(body['choices'][0]['message'], {'role': 'assistant', 'content': 'for i in range(3): print(i)'})
        self.assertEqual(body['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(body['usage'], {'prompt_tokens': 42, 'completion_tokens': 7, 'total_tokens': 49})

    def test_the_reasoning_lane_returns_its_thinking_separately(self):
        self.fake({'/api/chat': lambda body: chat_events('42', thinking='let me think')})
        kind, status, body = vscode.handle('POST', '/v1/chat/completions', {'model': 'sage-reason', 'messages': [{'role': 'user', 'content': '6*7?'}]}, 'c')
        message = body['choices'][0]['message']
        self.assertEqual((message['content'], message['reasoning_content']), ('42', 'let me think'))

    def test_the_reasoning_lane_always_has_room_for_the_answer_after_thinking(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('ok')})
        vscode.handle('POST', '/v1/chat/completions', {'model': 'sage-reason', 'max_tokens': 300, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        vscode.handle('POST', '/v1/chat/completions', {'model': 'sage-coder', 'max_tokens': 300, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        self.assertEqual([b['options']['num_predict'] for b in fake.bodies('/api/chat')], [2048, 300])

    def test_options_are_passed_on_and_the_context_size_is_fixed(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('ok')})
        self.chat(temperature=0.3, top_p=0.8, stop=['\n\n'], max_tokens=100)
        options = fake.bodies('/api/chat')[0]['options']
        self.assertEqual((options['temperature'], options['top_p'], options['stop'], options['num_predict'], options['num_ctx']), (0.3, 0.8, ['\n\n'], 100, 32000))

    def test_content_parts_and_developer_messages_are_flattened(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('ok')})
        vscode.handle('POST', '/v1/chat/completions', {'messages': [{'role': 'developer', 'content': 'rules'},
                      {'role': 'user', 'content': [{'type': 'text', 'text': 'look at'}, {'type': 'text', 'text': 'this'}]}]}, 'c')
        self.assertEqual([m['role'] for m in fake.bodies('/api/chat')[0]['messages']], ['system', 'user'])
        self.assertEqual(fake.bodies('/api/chat')[0]['messages'][1]['content'], 'look at\nthis')

    def test_tool_calls_are_translated_both_ways(self):
        calls = [{'function': {'name': 'read_file', 'arguments': {'path': 'a.py'}}}]
        fake = self.fake({'/api/chat': lambda body: chat_events('', calls=calls)})
        tools = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'read', 'parameters': {'type': 'object'}}}]
        kind, status, body = self.chat(tools=tools)
        reply = body['choices'][0]
        self.assertEqual(reply['finish_reason'], 'tool_calls')
        call = reply['message']['tool_calls'][0]
        self.assertEqual((call['type'], call['function']['name'], json.loads(call['function']['arguments'])), ('function', 'read_file', {'path': 'a.py'}))
        self.assertEqual(fake.bodies('/api/chat')[0]['tools'], tools)
        history = [{'role': 'user', 'content': 'read a.py'}, {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                   {'role': 'tool', 'tool_call_id': call['id'], 'name': 'read_file', 'content': 'print(1)'}]
        vscode.handle('POST', '/v1/chat/completions', {'messages': history}, 'c')
        sent = fake.bodies('/api/chat')[1]['messages']
        self.assertEqual(sent[1]['tool_calls'], [{'function': {'name': 'read_file', 'arguments': {'path': 'a.py'}}}])
        self.assertEqual((sent[2]['role'], sent[2]['tool_name'], sent[2]['content']), ('tool', 'read_file', 'print(1)'))

    def test_a_request_that_would_be_silently_truncated_is_refused(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('never')})
        with self.assertRaises(vscode.ApiError) as raised:
            vscode.handle('POST', '/v1/chat/completions', {'messages': [{'role': 'user', 'content': 'x' * 40000}]}, 'c')
        self.assertEqual((raised.exception.status, raised.exception.code), (400, 'context_length_exceeded'))
        self.assertEqual(fake.calls, [])                                       # Ollama is never asked to drop the start of the prompt

    def test_bad_input_and_upstream_failures_are_reported_clearly(self):
        self.fake({'/api/chat': lambda body: [OSError('connection refused')]})
        for body in ({}, {'messages': []}, {'messages': ['hi']}):
            with self.assertRaises(vscode.ApiError) as raised:
                vscode.handle('POST', '/v1/chat/completions', body, 'c')
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(vscode.ApiError) as raised:
            self.chat()
        self.assertEqual((raised.exception.status, raised.exception.code), (503, 'upstream_unavailable'))

    def test_streaming_sends_deltas_a_finish_chunk_and_done(self):
        self.fake({'/api/chat': lambda body: chat_events('hello world', thinking='hmm')})
        kind, stream = vscode.handle('POST', '/v1/chat/completions', {'model': 'sage-reason', 'stream': True, 'stream_options': {'include_usage': True},
                                                                         'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        self.assertEqual(kind, 'sse')
        lines = [l for l in ''.join(stream).split('\n\n') if l]
        self.assertEqual(lines[-1], 'data: [DONE]')
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        self.assertEqual(chunks[0]['choices'][0]['delta'], {'role': 'assistant', 'content': ''})
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '') for c in chunks if c['choices']), 'hello world')
        self.assertEqual(''.join(c['choices'][0]['delta'].get('reasoning_content', '') for c in chunks if c['choices']), 'hmm')
        finish = [c for c in chunks if c['choices'] and c['choices'][0]['finish_reason']]
        self.assertEqual(finish[0]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(chunks[-1]['usage']['total_tokens'], 49)

    def test_closing_a_stream_stops_ollama_and_frees_the_slot(self):
        closed = []

        def transport(path, body, cancel=None, timeout=600):
            try:
                for event in chat_events('a long answer that keeps going and going'):
                    yield event
            finally:
                closed.append(True)
        before = vscode._SLOTS._value
        with patch('sage.vscode._stream', transport):
            kind, stream = vscode.handle('POST', '/v1/chat/completions', {'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
            self.assertEqual(vscode._SLOTS._value, before - 1)
            next(stream)
            next(stream)
            stream.close()                                                     # the editor disconnected
        self.assertEqual(closed, [True])
        self.assertEqual(vscode._SLOTS._value, before)
        kind, never_started = None, None
        with patch('sage.vscode._stream', transport):
            kind, never_started = vscode.handle('POST', '/v1/chat/completions', {'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
            never_started.close()
        self.assertEqual(vscode._SLOTS._value, before)                          # even a stream that never started gives its slot back

    def test_streamed_tool_calls_carry_an_index(self):
        calls = [{'function': {'name': 'run', 'arguments': {'cmd': 'ls'}}}]
        self.fake({'/api/chat': lambda body: chat_events('', calls=calls)})
        kind, stream = vscode.handle('POST', '/v1/chat/completions', {'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}, 'c')
        chunks = [json.loads(l[6:]) for l in ''.join(stream).split('\n\n') if l.startswith('data: {')]
        deltas = [c['choices'][0]['delta'] for c in chunks if c['choices']]
        tool = next(d['tool_calls'][0] for d in deltas if 'tool_calls' in d)
        self.assertEqual((tool['index'], tool['function']['name']), (0, 'run'))
        self.assertEqual([c['choices'][0]['finish_reason'] for c in chunks if c['choices']][-1], 'tool_calls')


class InlineTests(Base):
    PREFIX = 'def add(a, b):\n    return '
    SUFFIX = '\n\nprint(add(1, 2))\n'

    def inline(self, **kwargs):
        defaults = {'prefix': self.PREFIX, 'suffix': self.SUFFIX, 'client': 'editor'}
        defaults.update(kwargs)
        return vscode.inline_complete(**defaults)

    def test_native_fill_in_the_middle_is_used_when_the_model_supports_it(self):
        fake = self.fake({'/api/generate': lambda body: [{'response': 'a + ', 'done': False}, {'response': 'b', 'done': True}]}, capabilities={'completion', 'insert'})
        result = self.inline()
        self.assertEqual((result['text'], result['method']), ('a + b', 'native'))
        body = fake.bodies('/api/generate')[0]
        self.assertEqual((body['model'], body['prompt'], body['suffix'], body['think']), ('local/sage-fast', self.PREFIX, self.SUFFIX, False))
        self.assertEqual(fake.bodies('/api/chat'), [])

    def test_a_chat_prompt_with_a_cursor_marker_is_used_otherwise(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('a + b')}, capabilities={'completion'})
        result = self.inline(language='python', path='math.py')
        self.assertEqual((result['text'], result['method']), ('a + b', 'chat'))
        body = fake.bodies('/api/chat')[0]
        self.assertFalse(body['think'])
        self.assertIn('<CURSOR>', body['messages'][1]['content'])
        self.assertIn('return <CURSOR>\n\nprint(add(1, 2))', body['messages'][1]['content'])
        self.assertIn('math.py (python)', body['messages'][1]['content'])
        self.assertEqual(body['messages'][0]['role'], 'system')

    def test_a_model_that_refuses_fill_in_the_middle_falls_back_and_is_remembered(self):
        fake = self.fake({'/api/generate': lambda body: [vscode.UpstreamError(400, '"local/sage-fast" does not support insert')],
                          '/api/chat': lambda body: chat_events('a + b')}, capabilities={'completion', 'insert'})
        self.assertEqual(self.inline()['method'], 'chat')
        self.assertEqual(self.inline(prefix=self.PREFIX + 'x')['method'], 'chat')
        self.assertEqual(len(fake.bodies('/api/generate')), 1)                  # not tried again

    def test_the_answer_is_cleaned_before_it_is_returned(self):
        clean = vscode.clean_completion
        self.assertEqual(clean('```python\na + b\n```', self.PREFIX, self.SUFFIX, True), 'a + b')
        self.assertEqual(clean('    return a + b', self.PREFIX, '', True), 'a + b')                       # the repeated line is removed
        self.assertEqual(clean('a + b\n\nprint(add(1, 2))', self.PREFIX, self.SUFFIX, False), 'a + b')     # the code after the cursor is not repeated
        self.assertEqual(clean('a<CURSOR> + b', self.PREFIX, '', True), 'a + b')
        self.assertEqual(clean('line1\nline2\nline3', 'x = ', '', False, max_lines=2), 'line1\nline2')
        self.assertEqual(clean('   \n  ', 'x = ', '', False), '')

    def test_only_the_rest_of_the_line_is_completed_when_code_follows_on_the_same_line(self):
        self.fake({'/api/chat': lambda body: chat_events('a + b\nreturn 0')}, capabilities=None)
        result = self.inline(prefix='print(add(', suffix='))\n')
        self.assertEqual(result['text'], 'a + b')
        result = self.inline(prefix='def f():\n    ', suffix='\n')
        self.assertEqual(result['text'], 'a + b\nreturn 0')                         # nothing after the cursor: several lines are fine

    def test_the_same_request_is_answered_from_the_cache(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('a + b')}, capabilities=None)
        first, second = self.inline(), self.inline()
        self.assertEqual((first['cached'], second['cached'], second['text']), (False, True, 'a + b'))
        self.assertEqual(len(fake.bodies('/api/chat')), 1)

    def test_an_empty_file_costs_nothing(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('never')}, capabilities=None)
        self.assertEqual(self.inline(prefix='  ', suffix='')['text'], '')
        self.assertEqual(fake.calls, [])

    def test_the_context_sent_to_the_model_is_bounded(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('x')}, capabilities=None)
        with patch.object(config, 'VSCODE_PREFIX_CHARS', 100), patch.object(config, 'VSCODE_SUFFIX_CHARS', 50):
            self.inline(prefix='p' * 5000 + 'END', suffix='s' * 5000)
        content = fake.bodies('/api/chat')[0]['messages'][1]['content']
        self.assertIn('p' * 97 + 'END<CURSOR>' + 's' * 50, content)
        self.assertLess(len(content), 300)

    def test_a_newer_request_from_the_same_editor_cancels_the_older_one(self):
        started, release = threading.Event(), threading.Event()

        def slow(body):
            started.set()
            for piece in ('a', ' + ', 'b'):
                release.wait(2)
                yield {'message': {'content': piece}, 'done': False}
        self.fake({'/api/chat': slow}, capabilities=None)
        results = {}
        first = threading.Thread(target=lambda: results.update(first=self.inline(prefix='def a(x):\n    return ')))
        first.start()
        self.assertTrue(started.wait(2))
        self.fake({'/api/chat': lambda body: chat_events('newer')}, capabilities=None)
        newer = self.inline(prefix='def a(x):\n    return x')
        release.set()
        first.join(3)
        self.assertEqual(newer['text'], 'newer')
        self.assertTrue(results['first']['superseded'])
        self.assertEqual(results['first']['text'], '')

    def test_different_editors_do_not_cancel_each_other(self):
        self.fake({'/api/chat': lambda body: chat_events('ok')}, capabilities=None)
        a = self.inline(client='editor-a', prefix='alpha = ')
        b = self.inline(client='editor-b', prefix='beta = ')
        self.assertFalse(a['superseded'] or b['superseded'])

    def test_the_openai_completions_endpoint_maps_prompt_and_suffix(self):
        fake = self.fake({'/api/chat': lambda body: chat_events('a + b')}, capabilities=None)
        kind, status, body = vscode.handle('POST', '/v1/completions', {'model': 'anything', 'prompt': self.PREFIX, 'suffix': self.SUFFIX, 'max_tokens': 64}, 'c')
        self.assertEqual((kind, body['object'], body['choices'][0]['text'], body['model']), ('json', 'text_completion', 'a + b', 'sage-fast'))
        self.assertEqual(fake.bodies('/api/chat')[0]['options']['num_predict'], 64)
        kind, stream = vscode.handle('POST', '/v1/completions', {'prompt': self.PREFIX + 'q', 'suffix': self.SUFFIX, 'stream': True}, 'c')
        events = [l for l in ''.join(stream).split('\n\n') if l]
        self.assertEqual(json.loads(events[0][6:])['choices'][0]['text'], 'a + b')
        self.assertEqual(events[-1], 'data: [DONE]')

    def test_the_editor_endpoint_returns_a_completion_with_its_timing(self):
        self.fake({'/api/chat': lambda body: chat_events('a + b')}, capabilities=None)
        kind, status, body = vscode.handle('POST', '/api/vscode/inline', {'prefix': self.PREFIX, 'suffix': self.SUFFIX, 'language': 'python', 'path': 'a.py'}, 'c')
        self.assertEqual((body['completion'], body['model'], body['method']), ('a + b', 'sage-fast', 'chat'))
        self.assertIn('ms', body)

    def test_bad_numbers_are_a_client_error(self):
        self.fake({'/api/chat': lambda body: chat_events('x')}, capabilities=None)
        with self.assertRaises(vscode.ApiError) as raised:
            vscode.handle('POST', '/api/vscode/inline', {'prefix': 'x = ', 'max_tokens': 'many'}, 'c')
        self.assertEqual(raised.exception.status, 400)

    def test_too_many_concurrent_requests_get_a_429(self):
        taken = [vscode._SLOTS.acquire(blocking=False) for _ in range(config.VSCODE_MAX_CONCURRENT)]
        try:
            self.fake({'/api/chat': lambda body: chat_events('x')}, capabilities=None)
            with self.assertRaises(vscode.ApiError) as raised:
                self.inline(prefix='unique prefix for this test = ')
            self.assertEqual(raised.exception.status, 429)
        finally:
            for ok in taken:
                if ok:
                    vscode._SLOTS.release()


class EmbeddingTests(Base):
    def test_embeddings_use_the_configured_embedding_model(self):
        seen = {}

        def fake_post(path, body, timeout=120):
            seen.update(path=path, body=body)
            return {'embeddings': [[0.1, 0.2], [0.3, 0.4]], 'prompt_eval_count': 9}
        with patch('sage.vscode._post_json', fake_post):
            kind, status, body = vscode.handle('POST', '/v1/embeddings', {'input': ['a', 'b']}, 'c')
        self.assertEqual(seen['path'], '/api/embed')
        self.assertEqual([d['embedding'] for d in body['data']], [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(body['usage']['total_tokens'], 9)
        with self.assertRaises(vscode.ApiError):
            vscode.handle('POST', '/v1/embeddings', {'input': []}, 'c')

    def test_status_reports_lanes_and_the_inline_method(self):
        with patch('sage.vscode._capabilities', lambda model: {'insert'}):
            kind, status, body = vscode.handle('GET', '/api/vscode/status', {}, 'c')
        self.assertEqual(body['inline'], {'lane': 'sage-fast', 'method': 'native', 'prefix_chars': 6000, 'suffix_chars': 1500})
        self.assertEqual(body['lanes']['sage-reason'], {'model': 'local/sage-reason', 'think': True})


class HttpTests(Base):
    """The same endpoints through the real server: token, login-cookie exemption, JSON errors and SSE framing."""
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault', WORKSPACE=root / 'workspace')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, token=TOKEN, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        if token is not None:
            req.add_header('Authorization', f'Bearer {token}')
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode()

    def test_a_token_is_required(self):
        self.assertEqual(self.request('GET', '/v1/models')[0], 200)
        status, _, body = self.request('GET', '/v1/models', token='wrong')
        self.assertEqual((status, json.loads(body)['error']['code']), (401, 'invalid_api_key'))
        self.assertEqual(self.request('GET', '/v1/models', token=None)[0], 401)
        self.assertEqual(self.request('POST', '/v1/chat/completions', {'messages': []}, token=None)[0], 401)

    def test_the_endpoints_are_disabled_until_a_token_is_configured(self):
        with patch.object(config, 'VSCODE_TOKEN', ''):
            status, _, body = self.request('GET', '/v1/models')
        self.assertEqual((status, json.loads(body)['error']['code']), (503, 'disabled'))

    def test_they_work_without_the_browser_login_cookie_even_when_a_password_is_set(self):
        with patch.object(config, 'PASSWORD', 'a-password'):
            self.assertEqual(self.request('GET', '/v1/models')[0], 200)
            self.assertEqual(self.request('GET', '/api/status')[0], 401)       # the rest of Sage still needs the login

    def test_errors_are_openai_shaped_json(self):
        status, _, body = self.request('POST', '/v1/chat/completions', {'messages': []})
        self.assertEqual(status, 400)
        self.assertIn('message', json.loads(body)['error'])
        self.assertEqual(self.request('GET', '/v1/nothing')[0], 404)

    def test_chat_completions_over_http(self):
        self.fake({'/api/chat': lambda body: chat_events('hello')})
        status, headers, body = self.request('POST', '/v1/chat/completions', {'model': 'sage-coder', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual((status, json.loads(body)['choices'][0]['message']['content']), (200, 'hello'))

    def test_streaming_over_http_is_server_sent_events(self):
        self.fake({'/api/chat': lambda body: chat_events('hello world')})
        status, headers, body = self.request('POST', '/v1/chat/completions', {'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/event-stream'))
        events = [l for l in body.split('\n\n') if l]
        self.assertEqual(events[-1], 'data: [DONE]')
        text = ''.join(json.loads(e[6:])['choices'][0]['delta'].get('content', '') for e in events[:-1])
        self.assertEqual(text, 'hello world')

    def test_inline_completion_over_http(self):
        self.fake({'/api/chat': lambda body: chat_events('a + b')}, capabilities=None)
        status, _, body = self.request('POST', '/api/vscode/inline', {'prefix': 'def add(a, b):\n    return ', 'suffix': '\n'}, headers={'X-Client-Id': 'vscode-1'})
        self.assertEqual((status, json.loads(body)['completion']), (200, 'a + b'))

    def test_an_invalid_body_is_a_400(self):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/chat/completions', data=b'not json', method='POST')
        req.add_header('Authorization', f'Bearer {TOKEN}')
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(raised.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
