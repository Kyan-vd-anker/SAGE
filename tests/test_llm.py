import json
import unittest
from unittest.mock import MagicMock, patch

from sage import llm


class LlmTests(unittest.TestCase):
    def test_chat_message_uses_longer_default_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {'message': {'content': 'answer'}}
        ).encode()

        with patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            result = llm.chat_message([{'role': 'user', 'content': 'question'}],model='test-model')

        self.assertEqual(result, {'content': 'answer'})
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 600)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['model'],'test-model')

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
