from io import BytesIO
import json
import tempfile
from urllib.error import HTTPError
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sage import llm


class LlmTests(unittest.TestCase):
    def test_chat_message_refuses_a_prompt_that_exceeds_its_context_window(self):
        with patch.object(llm._config,'NUM_CTX',128), patch('sage.llm.urllib.request.urlopen') as urlopen:
            with self.assertRaisesRegex(llm.OllamaError,'context window'):
                llm.chat_message([{'role':'user','content':'x'*360}],max_tokens=10,model='test-model')
        urlopen.assert_not_called()

    def test_chat_message_uses_longer_default_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'message': {'content': 'answer'}}
        ).encode()

        with patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            result = llm.chat_message([{'role': 'user', 'content': 'question'}],model='test-model')

        self.assertEqual(result, {'content': 'answer'})
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 600)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent['model'], 'test-model')
        self.assertEqual(sent['keep_alive'], llm._config.OLLAMA_KEEP_ALIVE)

    def test_chat_message_prompt_callback_receives_the_exact_request(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'answer'}}).encode()
        captured = []
        with patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            llm.chat_message([{'role': 'user', 'content': 'question'}], model='test-model', on_request=captured.append)
        self.assertEqual(captured, [json.loads(urlopen.call_args.args[0].data)])

    def test_chat_message_prompt_callback_includes_tool_fallback_request(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': '{"tool":"none","arguments":{},"response":"answer"}'}}).encode()
        tool = {'type': 'function', 'function': {'name': 'test_tool', 'parameters': {'type': 'object'}}}
        native_error = HTTPError('http://localhost/api/chat', 400, 'unsupported', None,
                                 BytesIO(b'model does not support tools'))
        captured = []
        with patch('sage.llm.urllib.request.urlopen', side_effect=[native_error, response]) as urlopen, \
                patch('sage.tools.definitions_for_query', return_value=[tool]):
            llm.chat_message([{'role': 'user', 'content': 'question'}], tools=[tool], model='test-model',
                             on_request=captured.append)
        sent = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        self.assertEqual(captured, sent)

    def test_chat_message_can_unload_ollama_models_after_the_request(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'answer'}}).encode()
        with patch.object(llm._config, 'OLLAMA_KEEP_ALIVE', '0'), patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            llm.chat_message([{'role': 'user', 'content': 'question'}], model='test-model')
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['keep_alive'], '0')

    def test_llamacpp_chat_message_persists_the_server_slot_snapshot(self):
        chat_response = MagicMock()
        chat_response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'answer'}}],
            'usage': {'prompt_tokens': 40, 'completion_tokens': 2},
            'timings': {'cache_n': 20, 'prompt_ms': 10, 'predicted_ms': 5},
        }).encode()
        save_response = MagicMock()
        save_response.__enter__.return_value.read.return_value = b'{"n_saved": 40}'
        captured = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), \
                patch.object(llm._config, 'LLAMA_CPP_CACHE_DIR', Path(tmp)), \
                patch.object(llm.llamacpp, '_cache_restore_attempted', set()), \
                patch('sage.llm.urllib.request.urlopen', side_effect=[chat_response, save_response]) as urlopen:
            reply = llm.chat_message([{'role': 'user', 'content': 'question'}], model=llm.MODEL, on_request=captured.append)

        calls = urlopen.call_args_list
        body = json.loads(calls[0].args[0].data)
        self.assertEqual(reply['content'], 'answer')
        self.assertEqual(body['model'], llm._config.LLAMA_CPP_MODEL)
        self.assertEqual(body['id_slot'], llm._config.LLAMA_CPP_SLOT)
        self.assertIn('/v1/chat/completions', calls[0].args[0].full_url)
        self.assertIn('/slots/0?action=save', calls[1].args[0].full_url)
        self.assertEqual(captured, [body])

    def test_llamacpp_stream_uses_fast_endpoint_for_fast_lane(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
            b'data: [DONE]\n',
        ]
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), \
                patch.object(llm._config, 'FAST_MODEL', 'fast-lane'), \
                patch.object(llm._config, 'LLAMA_CPP_FAST_URL', 'http://127.0.0.1:8081'), \
                patch.object(llm.llamacpp, '_cache_restore_attempted', set()), \
        patch('sage.llm.http.client.HTTPConnection', return_value=conn) as factory:
            result = llm.chat_stream([{'role': 'user', 'content': 'hi'}], model='fast-lane', cache_session=False)
            self.assertEqual(llm.display_model('fast-lane'), llm._config.LLAMA_CPP_FAST_MODEL)

        self.assertEqual(result['content'], 'answer')
        factory.assert_called_once_with('127.0.0.1', 8081, timeout=600)
        self.assertEqual(conn.request.call_args.args[0], 'POST')
        self.assertEqual(conn.request.call_args.args[1], '/v1/chat/completions')
        self.assertEqual(conn.request.call_args.kwargs['headers']['Content-Type'], 'application/json')
        self.assertEqual(conn.getresponse.call_count, 1)

    def test_llamacpp_nonstream_fast_lane_saves_to_its_own_server(self):
        chat_response = MagicMock()
        chat_response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'answer'}}],
            'usage': {'prompt_tokens': 40, 'completion_tokens': 2},
            'timings': {'cache_n': 20, 'prompt_ms': 10, 'predicted_ms': 5},
        }).encode()
        save_response = MagicMock()
        save_response.__enter__.return_value.read.return_value = b'{"n_saved": 40}'
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), \
                patch.object(llm._config, 'FAST_MODEL', 'fast-lane'), \
                patch.object(llm._config, 'LLAMA_CPP_FAST_URL', 'http://127.0.0.1:8081'), \
                patch.object(llm._config, 'LLAMA_CPP_FAST_CACHE_DIR', Path(tmp)), \
                patch.object(llm.llamacpp, '_cache_restore_attempted', set()), \
                patch('sage.llm.urllib.request.urlopen', side_effect=[chat_response, save_response]) as urlopen:
            result = llm.chat_message([{'role': 'user', 'content': 'hi'}], model='fast-lane')

        calls = urlopen.call_args_list
        self.assertEqual(result['content'], 'answer')
        self.assertEqual(calls[0].args[0].full_url, 'http://127.0.0.1:8081/v1/chat/completions')
        self.assertEqual(calls[1].args[0].full_url, 'http://127.0.0.1:8081/slots/0?action=save')

    def test_llamacpp_status_reports_separate_lane_servers(self):
        def fake_urlopen(request, timeout):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = (
                b'{"models":[]}' if request.full_url.endswith('/api/tags') else b'{"status":"ok"}'
            )
            return response

        with patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), \
                patch.object(llm, 'MODEL', 'fast-lane'), \
                patch.object(llm, 'CODING_MODEL', 'coding-lane'), \
                patch.object(llm, 'REASONING_MODEL', 'reasoning-lane'), \
                patch.object(llm._config, 'FAST_MODEL', 'fast-lane'), \
                patch.object(llm._config, 'CODING_MODEL', 'coding-lane'), \
                patch.object(llm._config, 'REASONING_MODEL', 'reasoning-lane'), \
                patch.object(llm._config, 'LLAMA_CPP_URL', 'http://127.0.0.1:8080'), \
                patch.object(llm._config, 'LLAMA_CPP_FAST_URL', 'http://127.0.0.1:8081'), \
                patch.object(llm._config, 'LLAMA_CPP_FAST_MODEL', 'sage-fast'), \
                patch.object(llm._config, 'LLAMA_CPP_MODEL', 'sage-deep'), \
                patch('sage.llm.urllib.request.urlopen', side_effect=fake_urlopen) as urlopen:
            state = llm.status()

        self.assertTrue(state['online'])
        self.assertEqual([(lane['role'], lane['model']) for lane in state['lanes']], [
            ('Fast', 'sage-fast'), ('Coding', 'sage-deep'), ('Reasoning', 'sage-deep'),
        ])
        health_urls = {call.args[0].full_url for call in urlopen.call_args_list if call.args[0].full_url.endswith('/health')}
        self.assertEqual(health_urls, {'http://127.0.0.1:8080/health', 'http://127.0.0.1:8081/health'})

    def test_llamacpp_stream_parses_sse_and_can_skip_disk_persistence(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"search_memory","arguments":"{\\"query\\":\\"x\\"}"}}]}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":80,"completion_tokens":4},"timings":{"cache_n":40,"prompt_ms":20,"predicted_ms":10}}\n',
            b'data: [DONE]\n',
        ]
        conn = MagicMock()
        conn.getresponse.return_value = response
        captured = []
        with patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), \
                patch('sage.llm.http.client.HTTPConnection', return_value=conn), \
                patch('sage.llm.urllib.request.urlopen') as urlopen:
            reply = llm.chat_stream([{'role': 'user', 'content': 'question'}], model=llm.MODEL,
                                    on_request=captured.append, cache_session=False)

        body = json.loads(conn.request.call_args.kwargs['body'])
        self.assertEqual(reply['content'], 'answer')
        self.assertEqual(reply['tool_calls'][0]['function']['name'], 'search_memory')
        self.assertEqual(reply['_prompt_tokens'], 80)
        self.assertEqual(captured, [body])
        urlopen.assert_not_called()

    def test_chat_uses_longer_default_timeout(self):
        with patch('sage.llm.chat_message', return_value={'content': 'answer'}) as chat_message:
            result = llm.chat([{'role': 'user', 'content': 'question'}])

        self.assertEqual(result, 'answer')
        self.assertEqual(chat_message.call_args.kwargs['timeout'], 600)

    def test_chat_stream_uses_longer_default_timeout(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [b'{"message":{"content":"answer"},"done":true}\n']

        with patch('sage.llm.http.client.HTTPConnection', return_value=MagicMock(getresponse=lambda: response)) as connection:
            result = llm.chat_stream([{'role': 'user', 'content': 'question'}],model='test-model')

        self.assertEqual(result['content'],'answer')
        self.assertEqual(connection.call_args.kwargs['timeout'],600)

    def test_chat_stream_prompt_callback_receives_the_exact_request(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [b'{"message":{"content":"answer"},"done":true}\n']
        conn = MagicMock()
        conn.getresponse.return_value = response
        captured = []
        with patch('sage.llm.http.client.HTTPConnection', return_value=conn):
            llm.chat_stream([{'role': 'user', 'content': 'question'}], model='test-model', on_request=captured.append)
        sent = json.loads(conn.request.call_args.kwargs['body'])
        self.assertEqual(captured, [sent])

    def test_chat_stream_retries_without_think_when_model_rejects_the_option(self):
        rejected = MagicMock()
        rejected.status = 400
        rejected.read.return_value = b'{"error":"model does not support think"}'
        accepted = MagicMock()
        accepted.status = 200
        accepted.__iter__.return_value = [b'{"message":{"content":"hey"},"done":true}\n']
        connections = [MagicMock(), MagicMock()]
        connections[0].getresponse.return_value = rejected
        connections[1].getresponse.return_value = accepted
        captured = []

        with patch.object(llm._config, 'FAST_THINK', False), \
                patch('sage.llm.http.client.HTTPConnection', side_effect=connections):
            reply = llm.chat_stream([{'role': 'user', 'content': 'hey'}], model=llm.MODEL,
                                    on_request=captured.append)

        bodies = [json.loads(conn.request.call_args.kwargs['body']) for conn in connections]
        self.assertEqual(reply['content'], 'hey')
        self.assertIs(bodies[0]['think'], False)
        self.assertNotIn('think', bodies[1])
        self.assertEqual(captured, bodies)

    def test_chat_message_retries_without_think_when_model_rejects_the_option(self):
        unsupported = HTTPError('http://localhost/api/chat', 400, 'unsupported', None,
                                BytesIO(b'model does not support think'))
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'message': {'content': 'hey'}}
        ).encode()
        captured = []

        with patch.object(llm._config, 'FAST_THINK', False), \
                patch('sage.llm.urllib.request.urlopen', side_effect=[unsupported, response]) as urlopen:
            reply = llm.chat_message([{'role': 'user', 'content': 'hey'}], model=llm.MODEL,
                                     on_request=captured.append)

        bodies = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        self.assertEqual(reply['content'], 'hey')
        self.assertIs(bodies[0]['think'], False)
        self.assertNotIn('think', bodies[1])
        self.assertEqual(captured, bodies)

    def test_chat_stream_reserves_output_tokens_when_thinking_is_enabled(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [b'{"message":{"content":"answer"},"done":true}\n']
        conn = MagicMock()
        conn.getresponse.return_value = response

        with patch.object(llm._config, 'REASONING_THINK', True), \
                patch('sage.llm.http.client.HTTPConnection', return_value=conn):
            llm.chat_stream([{'role': 'user', 'content': 'question'}], max_tokens=700, model=llm.REASONING_MODEL)

        body = json.loads(conn.request.call_args.kwargs['body'])
        self.assertIs(body['think'], True)
        self.assertEqual(body['options']['num_predict'], 2048)

    def test_model_for_request_selects_coder_for_code_and_default_for_other_requests(self):
        self.assertEqual(llm.model_for_request('Write a Python function to add two numbers.'),llm.CODING_MODEL)
        self.assertEqual(llm.model_for_request('Help me plan a weekend project.'),llm.MODEL)

    def test_model_for_request_selects_coder_for_tool_use_and_deep_lane_for_reasoning(self):
        self.assertEqual(llm.model_for_request('What is on my calendar today?',use_tools=True),llm.CODING_MODEL)
        self.assertEqual(llm.model_for_request('Calculate the current from voltage and resistance.',use_tools=True),llm.REASONING_MODEL)
        self.assertEqual(llm.model_for_request('Talk through the trade-offs in this problem.',use_tools=True),llm.REASONING_MODEL)

    def test_fast_lane_handles_chat_and_deep_lane_takes_long_input(self):
        for text in ('Hi Sage, how are you?', 'Yes, go ahead.', 'What does the approval button do?', 'Which of those two did you mean?'):
            self.assertEqual(llm.model_for_request(text),llm.MODEL,text)
        self.assertEqual(llm.model_for_request('word '*(llm.ROUTER_LONG_INPUT_CHARS//4)),llm.CODING_MODEL)
        self.assertEqual(llm.model_for_request(None),llm.MODEL)

    def test_unrouted_background_calls_use_the_deep_lane_and_chat_uses_the_fast_lane(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'ok'}}).encode()
        with patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            llm.chat_message([{'role': 'user', 'content': 'hi'}])
            self.assertEqual(json.loads(urlopen.call_args.args[0].data)['model'],llm.MODEL)
            llm.chat_message([{'role': 'user', 'content': 'plan'}],tools=[{'type':'function','function':{'name':'x','parameters':{}}}],background=True)
            self.assertEqual(json.loads(urlopen.call_args.args[0].data)['model'],llm.CODING_MODEL)

    def test_baked_models_get_the_changing_system_text_behind_the_conversation(self):
        history = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}, {'role': 'user', 'content': 'what is on today?'}]
        messages = [{'role': 'system', 'content': 'STATIC rules. Today is Saturday.'}] + history
        with patch('sage.llm._static_prompt', return_value='STATIC rules.'):
            sent = llm._baked('sage-fast', messages)
            self.assertNotEqual(sent[0]['role'], 'system')
            self.assertEqual([m for m in sent if m['role'] != 'system'], history)
            extra = [m for m in sent if m['role'] == 'system']
            self.assertEqual(extra, [{'role': 'system', 'content': 'Today is Saturday.'}])
            self.assertEqual(sent[-1], history[-1])
            self.assertEqual(llm._baked('qwen2.5:3b', messages), messages)  # plain models are untouched
            only_static = [{'role': 'system', 'content': 'STATIC rules.'}] + history
            self.assertEqual(llm._baked('sage-coder', only_static), history)

    def test_baked_models_keep_other_prompts_free_of_the_stored_prompt(self):
        with patch('sage.llm._static_prompt', return_value='STATIC rules.'):
            own = [{'role': 'system', 'content': 'Summarise.'}, {'role': 'user', 'content': 'text'}]
            self.assertEqual(llm._baked('sage-fast', own), own)
            bare = [{'role': 'user', 'content': 'text'}]
            self.assertEqual(llm._baked('sage-fast', bare), [{'role': 'system', 'content': ''}] + bare)

    def test_baked_prompt_is_applied_to_the_request_sent_to_ollama(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'ok'}}).encode()
        messages = [{'role': 'system', 'content': 'STATIC rules. Extra.'}, {'role': 'user', 'content': 'hi'}]
        with patch('sage.llm._static_prompt', return_value='STATIC rules.'), patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            llm.chat_message(messages, model='sage-fast')
        sent = json.loads(urlopen.call_args.args[0].data)['messages']
        self.assertEqual(sent, [{'role': 'user', 'content': 'hi'}, {'role': 'system', 'content': 'Extra.'}])
        self.assertNotIn('STATIC rules.', json.dumps(sent))

    def test_speed_stats_average_tokens_per_second_overall_and_per_model(self):
        with patch.object(llm, '_SPEEDS', __import__('collections').deque(maxlen=200)):
            llm._record_speed('sage-fast', {'eval_count': 100, 'eval_duration': 2_000_000_000, 'prompt_eval_duration': 200_000_000})
            llm._record_speed('sage-fast', {'eval_count': 300, 'eval_duration': 4_000_000_000, 'prompt_eval_duration': 400_000_000})
            llm._record_speed('sage-fast', {'eval_count': 4, 'eval_duration': 1_000_000_000, 'load_duration': 3_000_000_000, 'prompt_eval_duration': 500_000_000})  # cold start
            llm._record_speed('sage-coder', {'eval_count': 50, 'eval_duration': 5_000_000_000, 'prompt_eval_duration': 500_000_000})
            llm._record_speed('x', {'eval_count': 2, 'eval_duration': 1_000_000_000, 'prompt_eval_duration': 100_000_000})  # too short for a speed, still has a first token time
            llm._record_speed('x', {})  # no timings
            stats = llm.speed_stats()
            self.assertEqual(stats['by_model']['sage-fast']['tokens_per_second'], 66.7)
            self.assertEqual((stats['by_model']['sage-fast']['first_token_seconds'], stats['by_model']['sage-fast']['cold_starts']), (0.3, 1))
            self.assertEqual(stats['by_model']['sage-coder']['first_token_seconds'], 0.5)
            self.assertEqual((stats['tokens_per_second'], stats['replies'], stats['first_token_seconds']), (40.9, 5, 0.3))
            self.assertEqual((stats['last_tokens_per_second'], stats['last_first_token_seconds']), (10.0, 0.1))
            self.assertEqual(stats['by_model']['sage-coder']['tokens_per_second'], 10.0)
        with patch.object(llm, '_SPEEDS', __import__('collections').deque(maxlen=200)):
            self.assertEqual(llm.speed_stats()['tokens_per_second'], None)

    def test_ollama_replies_feed_the_speed_average(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'ok'}, 'eval_count': 80, 'eval_duration': 2_000_000_000}).encode()
        with patch.object(llm, '_SPEEDS', __import__('collections').deque(maxlen=200)), patch('sage.llm.urllib.request.urlopen', return_value=response):
            llm.chat_message([{'role': 'user', 'content': 'hi'}], model='test-model')
            self.assertEqual(llm.speed_stats()['by_model']['test-model']['tokens_per_second'], 40.0)

    def test_status_lists_every_model_lane_with_its_install_state(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'models': [{'name': llm.MODEL}, {'name': llm.REASONING_MODEL}]}).encode()
        with patch('sage.llm.urllib.request.urlopen', return_value=response):
            lanes = {item['model']: item for item in llm.status()['lanes']}
        self.assertTrue(lanes[llm.MODEL]['installed'] and 'Fast' in lanes[llm.MODEL]['role'])
        self.assertFalse(lanes[llm.CODING_MODEL]['installed'])
        self.assertIn('Reasoning', lanes[llm.REASONING_MODEL]['role'])
        self.assertTrue(lanes[llm.REASONING_MODEL]['installed'])
        with patch('sage.llm.urllib.request.urlopen', side_effect=OSError('down')):
            offline = llm.status()
        self.assertFalse(offline['online'])
        self.assertEqual({item['installed'] for item in offline['lanes']}, {None})

    def test_status_recognizes_untagged_configured_models_as_latest(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'models': [{'name': f'{llm.MODEL}:latest'}, {'name': f'{llm.REASONING_MODEL}:latest'}]
        }).encode()
        with patch('sage.llm.urllib.request.urlopen', return_value=response):
            state = llm.status()
        lanes = {item['model']: item for item in state['lanes']}
        self.assertTrue(lanes[llm.MODEL]['installed'])
        self.assertTrue(lanes[llm.REASONING_MODEL]['installed'])
