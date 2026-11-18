import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sage import agent, config, db, llm

STATIC = 'STATIC rules.'


def show_response(payload):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


class PromptLayoutTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('sage.llm._static_prompt', return_value=STATIC)
        patcher.start()
        self.addCleanup(patcher.stop)
        cache = patch.object(llm, '_BAKED_CHECK', {})
        cache.start()
        self.addCleanup(cache.stop)

    def test_models_built_by_build_models_are_recognised_with_their_local_prefix(self):
        messages = [{'role': 'system', 'content': f'{STATIC} Today is Monday.'}, {'role': 'user', 'content': 'hi'}]
        sent = llm._baked('local/sage-fast', messages)
        self.assertNotIn(STATIC, json.dumps(sent))
        self.assertEqual(llm._baked('qwen2.5:3b', messages), messages)

    def test_the_changing_text_stays_put_while_a_tool_loop_adds_messages(self):
        base = [{'role': 'system', 'content': f'{STATIC} Today is Monday.'}, {'role': 'user', 'content': 'turn the lamp on'}]
        loop = base + [{'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'lamp'}}]}, {'role': 'tool', 'content': 'ok'}]
        first, second = llm._baked('sage-fast', base), llm._baked('sage-fast', loop)
        self.assertEqual(second[:len(first)], first)                       # each iteration only appends: the earlier prompt is a prefix

    def test_plain_models_keep_the_fixed_prompt_first_and_move_the_changing_text_behind_the_conversation(self):
        history = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}, {'role': 'user', 'content': 'what is on today?'}]
        sent = llm._split_dynamic([{'role': 'system', 'content': f'{STATIC}\n\nNotes: it is Monday 10:42.'}] + history)
        self.assertEqual(sent[0], {'role': 'system', 'content': STATIC})
        self.assertEqual(sent[1:3], history[:2])
        self.assertEqual(sent[3], {'role': 'system', 'content': 'Notes: it is Monday 10:42.'})
        self.assertEqual(sent[4], history[2])

    def test_llamacpp_combines_all_system_context_at_the_beginning(self):
        messages = [
            {'role': 'system', 'content': f'{STATIC}\n\nProject notes.'},
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer'},
            {'role': 'system', 'content': '2026-11-18T11:00:00+01:00', '_sage_preserve_position': True},
            {'role': 'user', 'content': 'follow-up'},
        ]
        with patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), patch.object(llm._config, 'CHAT_TURNS', 12):
            sent = llm._prepare('sage-fast', messages)

        self.assertEqual([message['role'] for message in sent], ['system', 'user', 'assistant', 'user'])
        self.assertIn('Project notes.', sent[0]['content'])
        self.assertIn('2026-11-18', sent[0]['content'])
        self.assertEqual(sent[1:3], messages[1:3])
        self.assertEqual(sent[3], messages[-1])

    def test_llamacpp_keeps_only_the_fixed_part_in_the_system_message_and_folds_the_rest_into_the_latest_question(self):
        messages = [
            {'role': 'system', 'content': f'{STATIC}\n\nCore profile.{llm.CACHE_BREAK}Project notes.\nNo tool is needed.'},
            {'role': 'user', 'content': 'first question'},
            {'role': 'assistant', 'content': 'first answer'},
            {'role': 'system', 'content': '2026-11-18T11:00:00+01:00', '_sage_preserve_position': True},
            {'role': 'user', 'content': 'follow-up'},
        ]
        later = messages[:3] + [{'role': 'system', 'content': '2026-11-18T11:05:00+01:00', '_sage_preserve_position': True},
                                {'role': 'user', 'content': 'follow-up'}]
        with patch.object(llm._config, 'LLM_BACKEND', 'llama_cpp'), patch.object(llm._config, 'PROMPT_CACHE', True):
            sent = llm._prepare('sage-fast', messages)
            sent_later = llm._prepare('sage-fast', later)

        self.assertEqual([message['role'] for message in sent], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(sent[0]['content'], f'{STATIC}\n\nCore profile.')
        self.assertEqual(sent[1:3], messages[1:3])
        self.assertIn('Project notes.', sent[3]['content'])
        self.assertIn('2026-11-18T11:00:00', sent[3]['content'])
        self.assertTrue(sent[3]['content'].endswith('LATEST USER MESSAGE:\nfollow-up'))
        self.assertEqual(sent_later[:3], sent[:3])      # a new clock time changes nothing in front of the latest question

    def test_a_tool_loop_does_not_move_the_split_text(self):
        base = [{'role': 'system', 'content': f'{STATIC}\n\nNotes.'}, {'role': 'user', 'content': 'q'}]
        loop = base + [{'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'x'}}]}, {'role': 'tool', 'content': 'r'}]
        first = llm._split_dynamic(base)
        self.assertEqual(llm._split_dynamic(loop)[:len(first)], first)

    def test_other_prompts_are_not_touched(self):
        own = [{'role': 'system', 'content': 'Summarise.'}, {'role': 'user', 'content': 'text'}]
        self.assertEqual(llm._split_dynamic(own), own)

    def test_the_rearranging_is_only_used_when_the_conversation_is_sent_or_the_model_holds_the_prompt(self):
        messages = [{'role': 'system', 'content': f'{STATIC}\n\nNotes.'}, {'role': 'user', 'content': 'q'}]
        with patch.object(llm._config, 'CHAT_TURNS', 0):
            self.assertEqual(llm._prepare('qwen2.5:3b', messages), messages)
        with patch.object(llm._config, 'CHAT_TURNS', 4):
            self.assertEqual(llm._prepare('qwen2.5:3b', messages)[0]['content'], STATIC)
        with patch.object(llm._config, 'PROMPT_CACHE', False), patch.object(llm._config, 'CHAT_TURNS', 4):
            self.assertEqual(llm._prepare('qwen2.5:3b', messages), messages)

    def test_a_model_built_from_an_older_prompt_is_not_trusted(self):
        with patch('sage.llm.urllib.request.urlopen', return_value=show_response({'system': 'OLD PROMPT'})) as urlopen:
            self.assertFalse(llm._baked_current('local/sage-fast'))
            self.assertFalse(llm._baked_current('local/sage-fast'))
            self.assertEqual(urlopen.call_count, 1)                           # checked once, then remembered
        messages = [{'role': 'system', 'content': f'{STATIC} Extra.'}, {'role': 'user', 'content': 'hi'}]
        with patch('sage.llm._baked_current', return_value=False), patch.object(llm._config, 'CHAT_TURNS', 0):
            self.assertEqual(llm._prepare('local/sage-fast', messages), messages)   # the full current prompt is sent instead

    def test_a_current_or_unknown_model_keeps_the_stored_prompt(self):
        with patch('sage.llm.urllib.request.urlopen', return_value=show_response({'system': f'  {STATIC}\n'})):
            self.assertTrue(llm._baked_current('local/sage-fast'))
        llm._BAKED_CHECK.clear()
        with patch('sage.llm.urllib.request.urlopen', side_effect=OSError('ollama is down')):
            self.assertTrue(llm._baked_current('local/sage-fast'))
        llm._BAKED_CHECK.clear()
        with patch('sage.llm.urllib.request.urlopen', return_value=show_response({'modelfile': f'FROM x\nSYSTEM """{STATIC}"""\n'})):
            self.assertTrue(llm._baked_current('local/sage-fast'))
        with patch.object(llm._config, 'PROMPT_CACHE', False):
            self.assertFalse(llm._baked_current('sage-fast'))
        self.assertFalse(llm._baked_current('qwen2.5:3b'))

    def test_tool_definitions_are_sent_in_a_fixed_order(self):
        tools = [{'type': 'function', 'function': {'name': name, 'parameters': {}}} for name in ('zeta', 'alpha', 'mid')]
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'message': {'content': 'ok'}}).encode()
        with patch('sage.llm.urllib.request.urlopen', return_value=response) as urlopen:
            llm.chat_message([{'role': 'user', 'content': 'hi'}], tools=tools, model='qwen2.5:3b')
        sent = json.loads(urlopen.call_args.args[0].data)['tools']
        self.assertEqual([t['function']['name'] for t in sent], ['alpha', 'mid', 'zeta'])

    def test_cache_reuse_is_reported_from_ollamas_own_counters(self):
        with patch.object(llm, '_SPEEDS', __import__('collections').deque(maxlen=200)):
            fresh = {'eval_count': 50, 'eval_duration': 1_000_000_000, 'prompt_eval_count': 2000, 'prompt_eval_duration': 3_000_000_000}
            reused = {'eval_count': 50, 'eval_duration': 1_000_000_000, 'prompt_eval_count': 200, 'prompt_eval_duration': 300_000_000}
            llm._record_speed('m', fresh, prompt_chars=6600)
            self.assertEqual(llm.speed_stats()['last_prompt_reuse_estimate'], 0.0)
            llm._record_speed('m', reused, prompt_chars=6600)
            stats = llm.speed_stats()
            self.assertEqual((stats['last_prompt_tokens_evaluated'], stats['last_prompt_reuse_estimate'], stats['avg_prompt_tokens_evaluated']), (200, 0.9, 1100))
            llm._record_speed('m', reused, prompt_chars=100)
            self.assertIsNone(llm.speed_stats()['last_prompt_reuse_estimate'])


class TranscriptWindowTests(unittest.TestCase):
    def test_the_window_start_only_moves_in_blocks(self):
        starts, previous = [], None
        for turns in range(1, 61):
            start = agent._window_start([100] * turns, max_turns=9, block=3, budget=10_000)
            self.assertLessEqual(turns - start, 9)
            self.assertEqual(start % 3, 0)
            if start != previous:
                starts.append((turns, start))
                previous = start
        self.assertEqual(starts[0], (1, 0))
        self.assertEqual([s for _, s in starts], list(range(0, 52, 3)))      # 18 moves in 60 turns, not 51 (one per turn)

    def test_the_character_budget_also_moves_the_window_in_blocks(self):
        sizes = [1000] * 20
        start = agent._window_start(sizes, max_turns=50, block=4, budget=6500)
        self.assertEqual(start % 4, 0)
        self.assertLessEqual(sum(sizes[start:]), 6500)
        self.assertGreater(sum(sizes[start - 4:]), 6500)

    def test_nothing_is_sent_when_even_the_newest_block_does_not_fit(self):
        self.assertEqual(agent._window_start([5000, 5000, 5000], max_turns=10, block=3, budget=1000), 3)


class AgentPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault', WORKSPACE=root / 'workspace',
                                 PLUGIN_DIR=Path(__file__).resolve().parents[1] / 'plugins')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def exchange(self, user, assistant, source_docs=None):
        db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)', ('user', user, db.now()))
        db.execute('INSERT INTO messages(role,content,created_at,source_docs) VALUES(?,?,?,?)', ('assistant', assistant, db.now(), source_docs))

    def ask(self, text, **kwargs):
        with patch('sage.agent.llm.chat_message', return_value={'content': 'ok'}) as model:
            agent.respond(text, **kwargs)
        return model.call_args.args[0]

    def test_the_default_prompt_is_unchanged_in_shape_but_ordered_by_how_often_it_changes(self):
        with patch('sage.agent._relevant_context', return_value=([{'role': 'user', 'content': 'old question'}], [{'content': 'a saved memory'}])), \
                patch('sage.agent.project_context', return_value={'name': 'ROV'}):
            sent = self.ask('Explain how a ballast tank works', project_id=1)
        self.assertEqual([m['role'] for m in sent], ['system', 'system', 'user'])
        system = sent[0]['content']
        self.assertTrue(system.startswith(agent.SYSTEM))
        order = [system.index(text) for text in ('active-project records', 'saved memories', 'historical conversation')]
        self.assertEqual(order, sorted(order))                                  # stable first, the clock last
        self.assertRegex(sent[-2]['content'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        self.assertTrue(sent[-2]['_sage_preserve_position'])

    def test_past_messages_are_sent_as_a_conversation_when_enabled(self):
        for n in range(3):
            self.exchange(f'question {n}', f'answer {n}')
        with patch.object(config, 'CHAT_TURNS', 6), patch('sage.agent._relevant_context', return_value=([], [])) as retrieved:
            sent = self.ask('and a follow-up?')
        self.assertEqual([m['role'] for m in sent], ['system'] + ['user', 'assistant'] * 3 + ['system', 'user'])
        self.assertEqual([m['content'] for m in sent[1:7:2]], ['question 0', 'question 1', 'question 2'])
        self.assertRegex(sent[-2]['content'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        self.assertEqual(sent[-1]['content'], 'and a follow-up?')
        self.assertEqual(retrieved.call_args.kwargs['recent_limit'], 0)          # they are real messages now, not quoted again as context

    def test_what_is_sent_to_ollama_keeps_the_conversation_as_an_unchanged_prefix_between_turns(self):
        for n in range(2):
            self.exchange(f'question {n}', f'answer {n}')
        with patch.object(config, 'CHAT_TURNS', 6), patch.object(config, 'CHAT_TURN_BLOCK', 3), patch('sage.llm._baked_current', return_value=False):
            first = llm._prepare('qwen2.5:3b', self.ask('third question'))
            second = llm._prepare('qwen2.5:3b', self.ask('fourth question'))      # respond() stored the third exchange (the model said 'ok')
        self.assertEqual(first[0], {'role': 'system', 'content': agent.SYSTEM})
        self.assertEqual(second[0], first[0])
        n = 1 + 4                                                                # the fixed prompt and two earlier exchanges
        self.assertEqual(second[:n], first[:n])
        self.assertEqual([m['content'] for m in second[n:n + 2]], ['third question', 'ok'])
        for sent in (first, second):                                             # the changing text sits in front of the latest question
            self.assertEqual(sent[-3]['role'], 'system')
            self.assertRegex(sent[-3]['content'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
            self.assertEqual(sent[-1]['role'], 'user')

    def test_llamacpp_prompt_keeps_the_conversation_as_an_unchanged_prefix_between_turns(self):
        for n in range(2):
            self.exchange(f'question {n}', f'answer {n}')
        with patch.object(config, 'LLM_BACKEND', 'llama_cpp'), patch.object(config, 'PROMPT_CACHE', True), \
                patch.object(config, 'CHAT_TURNS', 6), patch.object(config, 'CHAT_TURN_BLOCK', 3), \
                patch('sage.agent._relevant_context', return_value=([], [])):
            first = llm._prepare('sage-fast', self.ask('Explain how a ballast tank works'))
            second = llm._prepare('sage-fast', self.ask('Explain how a trim tank works'))
        self.assertEqual(first[0]['content'], agent.SYSTEM)                      # no core memories in this database
        self.assertEqual(second[:5], first[:5])                                  # the fixed prompt and the two earlier exchanges
        self.assertEqual([m['content'] for m in second[5:7]], ['Explain how a ballast tank works', 'ok'])
        for sent in (first, second):
            self.assertEqual(sent[-1]['role'], 'user')
            self.assertIn('No tool is needed', sent[-1]['content'])
            self.assertRegex(sent[-1]['content'], r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}')

    def test_the_window_moves_in_blocks_so_most_turns_keep_the_same_start(self):
        starts = []
        with patch.object(config, 'CHAT_TURNS', 6), patch.object(config, 'CHAT_TURN_BLOCK', 3):
            for n in range(24):
                self.exchange(f'question {n}', f'answer {n}')
                transcript = agent._transcript('next')
                self.assertLessEqual(len(transcript), 12)
                starts.append(transcript[0]['content'])
        self.assertLessEqual(len(set(starts)), 8)                                # 24 turns, a handful of moves instead of 18

    def test_answers_written_from_documents_and_unfinished_turns_are_left_out(self):
        self.exchange('about the paper', 'the paper says...', source_docs='abc123')
        self.exchange('plain question', 'plain answer')
        db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)', ('user', 'question that never got an answer', db.now()))
        with patch.object(config, 'CHAT_TURNS', 6):
            transcript = agent._transcript('current question')
        self.assertEqual([m['content'] for m in transcript], ['plain question', 'plain answer'])

    def test_a_long_message_is_cut_deterministically(self):
        self.exchange('x' * 5000, 'short answer')
        with patch.object(config, 'CHAT_TURNS', 6), patch.object(config, 'CHAT_MESSAGE_CHARS', 100):
            first = agent._transcript('next')
            second = agent._transcript('next')
        self.assertEqual(first, second)
        self.assertEqual(len(first[0]['content']), 101)
        self.assertTrue(first[0]['content'].endswith('\u2026'))

    def test_the_feature_is_off_by_default_and_by_the_safety_switch(self):
        self.exchange('question', 'answer')
        self.assertEqual(agent._transcript('next'), [])
        with patch.object(config, 'CHAT_TURNS', 6), patch.object(config, 'PROMPT_CACHE', False):
            self.assertEqual(agent._transcript('next'), [])


if __name__ == '__main__':
    unittest.main()
