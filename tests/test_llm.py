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
        self.assertEqual(urlopen.call_args.kwargs['timeout'], 300)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['model'],'test-model')

    def test_chat_uses_longer_default_timeout(self):
        with patch('sage.llm.chat_message', return_value={'content': 'answer'}) as chat_message:
            result = llm.chat([{'role': 'user', 'content': 'question'}])

        self.assertEqual(result, 'answer')
        self.assertEqual(chat_message.call_args.kwargs['timeout'], 300)

    def test_model_for_request_selects_coder_for_code_and_default_for_other_requests(self):
        self.assertEqual(llm.model_for_request('Write a Python function to add two numbers.'),llm.CODING_MODEL)
        self.assertEqual(llm.model_for_request('Help me plan a weekend project.'),llm.MODEL)

    def test_model_for_request_selects_coder_for_tool_use_but_default_for_reasoning(self):
        self.assertEqual(llm.model_for_request('What is on my calendar today?',use_tools=True),llm.CODING_MODEL)
        self.assertEqual(llm.model_for_request('Calculate the current from voltage and resistance.',use_tools=True),llm.MODEL)
        self.assertEqual(llm.model_for_request('Talk through the trade-offs in this problem.',use_tools=True),llm.MODEL)
