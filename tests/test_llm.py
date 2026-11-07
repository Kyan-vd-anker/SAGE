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
