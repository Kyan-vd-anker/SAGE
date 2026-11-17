import unittest
from unittest.mock import patch

from sage import agent, config


class ChatExplanationTests(unittest.TestCase):
    def test_explanation_is_a_concise_summary_and_never_returns_think_blocks(self):
        with patch('sage.agent.llm.chat_message', return_value={
            'content': '<think>private scratch work</think>- It answers the requested question.\n- It notes one assumption.',
        }) as model:
            summary = agent.explain_answer('Why is the sky blue?', 'Short answer about light scattering.')

        self.assertNotIn('private scratch work', summary)
        self.assertIn('requested question', summary)
        self.assertIn('private chain-of-thought', model.call_args.args[0][0]['content'])
        self.assertIn('Why is the sky blue?', model.call_args.args[0][1]['content'])
        self.assertEqual(model.call_args.kwargs['model'], config.FAST_MODEL)

    def test_explanation_rejects_empty_or_oversized_exchange(self):
        with patch('sage.agent.llm.chat_message') as model:
            with self.assertRaisesRegex(ValueError, 'question must contain'):
                agent.explain_answer('', 'An answer')
            with self.assertRaisesRegex(ValueError, 'answer must contain'):
                agent.explain_answer('A question', 'x' * 12001)

        model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
