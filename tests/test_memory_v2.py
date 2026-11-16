import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage import capture, config, db, entities, memory, userprofile


class MemoryV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault', WORKSPACE=root / 'workspace')
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = patch('sage.memory.semantic_ready', return_value=False)
        ready.start()
        self.addCleanup(ready.stop)
        db.init()

    def status(self, mid):
        return db.rows('SELECT status,tier,superseded_by,supersedes_id FROM memories WHERE id=?', (mid,))[0]

    # ---------------------------------------------------------------- supersede, never overwrite

    def test_a_shorter_near_duplicate_never_overwrites_a_richer_note(self):
        first = memory.save('Kyan runs SAGE on a Ryzen 5600X desktop with 32GB RAM and an NVMe drive')
        second = memory.save('Kyan runs SAGE on a Ryzen 5600X desktop')
        self.assertEqual(first, second)
        rows = db.rows('SELECT content FROM memories')
        self.assertEqual(len(rows), 1)
        self.assertIn('NVMe', rows[0]['content'])

    def test_a_changed_fact_supersedes_the_old_one_and_keeps_it(self):
        old = memory.save('Kyan runs SAGE on a laptop with 16GB RAM')
        new = memory.save('Kyan runs SAGE on a Ryzen 5600X desktop with 16GB RAM')
        self.assertNotEqual(old, new)
        self.assertEqual(self.status(old)['status'], 'superseded')
        self.assertEqual(self.status(old)['superseded_by'], new)
        self.assertEqual(self.status(new)['supersedes_id'], old)
        self.assertEqual([r['id'] for r in memory.list_memories('active')], [new])
        self.assertIn('Replaced by', (config.VAULT / f'Memory {old}.md').read_text(encoding='utf-8'))

    def test_a_suggested_change_only_replaces_the_old_note_once_approved(self):
        old = memory.save('Kyan runs SAGE on a laptop with 16GB RAM')
        proposal = memory.save('Kyan runs SAGE on a Ryzen 5600X desktop with 16GB RAM', status='suggested', quiet=True)
        self.assertEqual(self.status(old)['status'], 'active')
        self.assertEqual(self.status(proposal)['status'], 'suggested')
        memory.update_memory(proposal, status='active')
        self.assertEqual(self.status(old)['status'], 'superseded')
        self.assertEqual(self.status(proposal)['status'], 'active')

    def test_explicit_supersedes_id_is_honoured_and_inherits_tier_and_pin(self):
        old = memory.save("The user's name is Kyan", tier='core')
        memory.update_memory(old, pinned=True)
        new = memory.save('The user goes by Kyan van den Anker', supersedes=old)
        self.assertEqual(self.status(old)['status'], 'superseded')
        row = db.rows('SELECT tier,pinned FROM memories WHERE id=?', (new,))[0]
        self.assertEqual((row['tier'], row['pinned']), ('core', 1))

    def test_history_comes_back_through_recall(self):
        old = memory.save('Kyan runs SAGE on a laptop with 16GB RAM')
        new = memory.save('Kyan runs SAGE on a Ryzen 5600X desktop with 16GB RAM')
        plain = memory.recall('what does Kyan run SAGE on')
        self.assertEqual([m['id'] for m in plain['memories']], [new])
        full = memory.recall('what does Kyan run SAGE on', include_history=True)
        ids = [m['id'] for m in full['memories']]
        self.assertIn(old, ids)
        self.assertEqual(next(m for m in full['memories'] if m['id'] == new)['earlier'][0]['text'][:24], 'Kyan runs SAGE on a lapt')

    # ---------------------------------------------------------------- tiers

    def test_tiers_follow_the_kind_and_the_explicit_capture_styles(self):
        self.assertEqual(self.status(memory.save('User prefers short answers', 'preference'))['tier'], 'procedural')
        self.assertEqual(self.status(memory.save('Daily summary 2026-11-01: worked on the ROV', 'daily_summary'))['tier'], 'episodic')
        self.assertEqual(self.status(memory.save('The printer is an Ender 3 with a direct drive upgrade'))['tier'], 'semantic')
        saved = memory.capture('My name is Kyan')
        self.assertEqual(self.status(saved[0][0])['tier'], 'core')

    def test_working_notes_expire(self):
        mid = memory.save('The ROV thruster test is in progress on the bench', tier='working')
        self.assertTrue(db.rows('SELECT expires_at FROM memories WHERE id=?', (mid,))[0]['expires_at'])
        db.execute("UPDATE memories SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (mid,))
        self.assertEqual(memory.relevant('how is the ROV thruster test going on the bench', limit=3, semantic=False), [])
        memory.maintain()
        self.assertEqual(self.status(mid)['status'], 'archived')

    def test_episodic_notes_only_surface_when_the_question_is_about_the_past(self):
        memory.save('Daily summary 2026-11-01: decided on RS485 for the ROV tether', 'daily_summary')
        self.assertEqual(memory.relevant('which tether does the ROV use for RS485', limit=3, semantic=False), [])
        found = memory.relevant('what did we decide yesterday about the RS485 tether', limit=3, semantic=False)
        self.assertEqual(len(found), 1)

    # ---------------------------------------------------------------- entity graph

    def test_entities_resolve_by_alias_and_link_notes_automatically(self):
        eid = entities.ensure('ROV', 'project', 'Underwater vehicle build', aliases=['the rov', 'underwater vehicle'])
        self.assertEqual(entities.ensure('the ROV'), eid)
        self.assertIsNone(entities.ensure('the'))
        self.assertEqual(entities.find('How is the ROV doing? Also not the rover.'), [eid])
        mid = memory.save('The thrusters on the ROV are two Blue Robotics T200 units')
        self.assertEqual([r['entity_id'] for r in db.rows('SELECT entity_id FROM memory_entities WHERE memory_id=?', (mid,))], [eid])

    def test_a_linked_note_is_found_without_sharing_a_keyword_and_cards_dedupe(self):
        eid = entities.ensure('ROV', 'project')
        mid = memory.save('Propulsion uses two T200 thrusters', entity_names=['ROV'])
        self.assertEqual(memory.relevant('status of the other thing', limit=3, semantic=False), [])
        found = memory.relevant('how is the ROV going', limit=3, semantic=False, entity_ids=[eid])
        self.assertEqual([m['id'] for m in found], [mid])
        self.assertEqual(entities.cards_for_ids([eid], exclude={mid}), [])
        card = entities.cards_for_ids([eid])[0]
        self.assertEqual(card['facts'], ['Propulsion uses two T200 thrusters'])

    def test_relations_and_neighbours(self):
        rov = entities.ensure('ROV', 'project')
        nano = entities.ensure('Arduino Nano', 'device')
        entities.relate(rov, nano, 'uses')
        entities.relate(rov, nano, 'uses')
        self.assertEqual(entities.neighbors(rov)[0]['name'], 'Arduino Nano')
        self.assertEqual(db.rows('SELECT weight FROM entity_links')[0]['weight'], 1.5)
        self.assertIn('uses Arduino Nano', entities.card(rov)['related'])

    def test_projects_become_entities(self):
        db.execute("INSERT INTO projects(name,description,status,created_at) VALUES('ROV Underwater Vehicle Project','','active',?)", (db.now(),))
        ids = entities.find('how is the ROV going')
        self.assertEqual(len(ids), 1)
        self.assertEqual(entities.project_ids(ids), [1])

    def test_merge_moves_everything_to_the_target(self):
        a, b = entities.ensure('Rov project'), entities.ensure('Underwater thing')
        mid = memory.save('Some note about Underwater thing here', entity_names=['Underwater thing'])
        self.assertTrue(entities.merge(b, a))
        self.assertEqual(entities.resolve('underwater thing'), a)
        self.assertEqual(db.rows('SELECT entity_id FROM memory_entities WHERE memory_id=?', (mid,))[0]['entity_id'], a)

    # ---------------------------------------------------------------- core profile

    def test_core_block_is_stable_budgeted_and_excludes_non_core(self):
        memory.save("The user's name is Kyan", tier='core', importance=5)
        memory.save('User always wants answers in Dutch unless asked otherwise', 'preference', tier='core')
        memory.save('The printer is an Ender 3 with a direct drive upgrade')
        first, ids = userprofile.core()
        second, _ = userprofile.core()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(userprofile.HEADER))
        self.assertIn("Kyan", first)
        self.assertNotIn('Ender', first)
        self.assertEqual(len(ids), 2)
        with patch.object(config, 'CORE_PROFILE_CHARS', 160):
            small, small_ids = userprofile.core()
        self.assertLessEqual(len(small), 160)
        self.assertEqual(len(small_ids), 1)

    def test_core_block_lists_projects_and_pins_but_small_talk_skips_projects(self):
        db.execute("INSERT INTO projects(name,description,status,next_action,created_at) VALUES('ROV','','active','Wire the RS485 driver',?)", (db.now(),))
        pinned = memory.save('Never print on the bed without a brim for ABS')
        memory.update_memory(pinned, pinned=True)
        text, ids = userprofile.core()
        self.assertIn('Active projects: ROV (next: Wire the RS485 driver)', text)
        self.assertIn(pinned, ids)
        compact, _ = userprofile.core(compact=True, with_projects=False)
        self.assertNotIn('Active projects', compact)

    def test_empty_profile_is_empty(self):
        self.assertEqual(userprofile.core(), ('', ()))

    # ---------------------------------------------------------------- LLM capture

    def test_worth_capturing(self):
        self.assertTrue(capture.worth_capturing('I decided to use RS485 for the ROV tether instead of CAN.'))
        self.assertFalse(capture.worth_capturing('How do I wire RS485 on an Arduino Nano?'))
        self.assertFalse(capture.worth_capturing('hi'))
        self.assertFalse(capture.worth_capturing('my password is hunter2 and I use it everywhere'))

    def test_capture_validates_against_what_the_user_said(self):
        text = 'I decided to use RS485 for the ROV tether, and the control board is an Arduino Nano.'
        data = {
            'facts': [
                {'content': 'The user decided to use RS485 for the ROV tether', 'tier': 'episodic', 'kind': 'decision', 'importance': 4, 'confidence': 0.9, 'entities': ['ROV']},
                {'content': 'The user owns a Raspberry Pi cluster in the attic', 'tier': 'semantic', 'kind': 'fact', 'importance': 3, 'confidence': 0.95},
                {'content': 'The user is a licensed pilot with many hours', 'tier': 'core', 'kind': 'fact', 'importance': 5, 'confidence': 0.9},
                {'content': 'The ROV control board is an Arduino Nano', 'tier': 'core', 'kind': 'fact', 'importance': 3, 'confidence': 0.95},
            ],
            'entities': [{'name': 'ROV', 'kind': 'project', 'description': 'Underwater vehicle', 'aliases': ['the rov']},
                         {'name': 'Arduino Nano', 'kind': 'device'},
                         {'name': 'Moon Base', 'kind': 'place'}],
            'relations': [{'a': 'ROV', 'rel': 'uses', 'b': 'Arduino Nano'}, {'a': 'ROV', 'rel': 'at', 'b': 'Moon Base'}],
        }
        outcome = capture.apply(data, text)
        rows = db.rows('SELECT content,status,tier FROM memories ORDER BY id')
        self.assertEqual(len(rows), 2)                                   # the invented facts were dropped
        self.assertEqual((rows[0]['status'], rows[0]['tier']), ('active', 'episodic'))
        self.assertEqual((rows[1]['status'], rows[1]['tier']), ('suggested', 'core'))   # core always waits for approval
        self.assertIsNone(entities.resolve('Moon Base'))
        self.assertEqual(len(entities.neighbors(entities.resolve('ROV'))), 1)
        self.assertIn('1 suggestions', outcome)

    def test_capture_only_supersedes_memories_it_was_shown(self):
        old = memory.save('The ROV tether uses CAN bus between the surface and the vehicle')
        other = memory.save('Totally unrelated note about the printer bed temperature')
        text = 'We switched the ROV tether to RS485 instead of CAN bus between the surface and the vehicle.'
        data = {'facts': [{'content': 'The ROV tether now uses RS485 between the surface and the vehicle', 'tier': 'semantic', 'kind': 'fact',
                           'importance': 4, 'confidence': 0.9, 'supersedes': other}]}
        capture.apply(data, text, related_ids=[old])                      # `other` was never shown to the model
        self.assertEqual(self.status(other)['status'], 'active')
        data['facts'][0]['supersedes'] = old
        capture.apply(data, text, related_ids=[old])
        self.assertEqual(self.status(old)['status'], 'superseded')

    def test_queue_and_run_a_capture_job(self):
        text = 'I decided to use RS485 for the ROV tether instead of CAN, and the vehicle weighs about 4 kg.'
        job_id = capture.queue(text, 'Sounds good.')
        self.assertIsNotNone(job_id)
        self.assertIsNone(capture.queue(text, 'again'))                    # no duplicate while one is queued
        reply = json.dumps({'facts': [{'content': 'The user decided to use RS485 for the ROV tether instead of CAN', 'tier': 'episodic',
                                       'kind': 'decision', 'importance': 4, 'confidence': 0.9}], 'entities': [], 'relations': []})
        with patch('sage.llm.chat', return_value=reply):
            outcome = capture.run(db.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0])
        self.assertIn('Saved 1 memories', outcome)

    def test_capture_can_be_switched_off(self):
        with patch.object(config, 'CAPTURE_ENABLED', False):
            self.assertIsNone(capture.queue('I decided to use RS485 for the ROV tether instead of CAN.', ''))

    def test_backfill_enriches_old_memories_with_a_cursor(self):
        keep = memory.save('Kyan prefers using PlatformIO for the Arduino Nano firmware in the ROV')
        reply = json.dumps({'items': [{'id': keep, 'tier': 'procedural',
                                       'entities': [{'name': 'Arduino Nano', 'kind': 'device'}],
                                       'relations': []}]})
        with patch('sage.llm.chat', return_value=reply):
            capture.run_backfill()
        self.assertEqual(self.status(keep)['tier'], 'procedural')
        self.assertTrue(db.rows('SELECT 1 FROM memory_entities WHERE memory_id=?', (keep,)))
        self.assertEqual(memory._setting('memory_backfill_cursor'), str(keep))
        self.assertEqual(capture.run_backfill(), 'Every memory has been enriched.')

    # ---------------------------------------------------------------- project state of play

    def test_state_of_play_roundtrip_and_open_loops(self):
        pid = db.execute("INSERT INTO projects(name,description,status,created_at) VALUES('ROV','Build an underwater vehicle','active',?)", (db.now(),))
        self.assertEqual(userprofile.state_text(pid), '')
        db.execute("INSERT INTO messages(role,content,created_at) VALUES('user','The ROV thruster test passed today, next is the depth sensor.',?)", (db.now(),))
        reply = json.dumps({'summary': 'Thrusters work; the depth sensor is next.', 'next_step': 'Wire the depth sensor', 'open_loops': ['Pick a pressure sensor']})
        with patch('sage.llm.chat', return_value=reply):
            self.assertIn('Updated', userprofile.refresh_state(pid))
        text = userprofile.state_text(pid)
        self.assertIn('Thrusters work', text)
        self.assertIn('Next: Wire the depth sensor', text)
        self.assertIn('Open: Pick a pressure sensor', text)
        userprofile.merge_open_loops(pid, ['Order the tether', 'pick a pressure sensor'])
        self.assertEqual(json.loads(userprofile.get_state(pid)['open_loops']), ['Pick a pressure sensor', 'Order the tether'])
        with patch('sage.llm.chat') as model:                              # nothing new was said: no model call
            self.assertIn('unchanged', userprofile.refresh_state(pid))
        model.assert_not_called()

    def test_queue_refresh_is_deduplicated_and_throttled(self):
        pid = db.execute("INSERT INTO projects(name,description,status,created_at) VALUES('ROV','','active',?)", (db.now(),))
        first = userprofile.queue_refresh(pid)
        self.assertIsNotNone(first)
        self.assertIsNone(userprofile.queue_refresh(pid))
        db.execute("UPDATE jobs SET status='completed' WHERE id=?", (first,))
        db.execute("INSERT INTO project_state(project_id,summary,updated_at) VALUES(?,?,?)", (pid, 'fresh', db.now()))
        self.assertIsNone(userprofile.queue_refresh(pid))                  # refreshed a moment ago

    # ---------------------------------------------------------------- recall tool

    def test_recall_combines_memories_history_decisions_and_entities(self):
        entities.ensure('ROV', 'project', 'Underwater vehicle')
        memory.save('The ROV tether uses RS485 between the surface and the vehicle', entity_names=['ROV'])
        db.execute("INSERT INTO messages(role,content,created_at) VALUES('user','We talked about the ROV tether and RS485 last week.',?)", (db.now(),))
        db.execute("INSERT INTO decision_log(title,decision,rationale,created_at) VALUES('ROV tether','Use RS485','Long cable runs',?)", (db.now(),))
        result = memory.recall('what did we decide about the ROV tether')
        self.assertEqual(len(result['memories']), 1)
        self.assertEqual(len(result['history']), 1)
        self.assertEqual(result['decisions'][0]['decision'], 'Use RS485')
        self.assertEqual(result['entities'][0]['name'], 'ROV')
        self.assertLess(len(json.dumps(result)), 6000)
        self.assertIn('note', memory.recall('quantum cheese soufflé'))

    def test_recall_filters_by_tier_and_age(self):
        memory.save('Daily summary 2026-11-01: wired the ROV tether', 'daily_summary')
        memory.save('The ROV tether is 50 metres of shielded cable')
        self.assertEqual(len(memory.recall('ROV tether', tier='episodic')['memories']), 1)
        db.execute("UPDATE memories SET updated_at='2020-01-01T00:00:00+00:00'")
        self.assertEqual(memory.recall('ROV tether', days=30)['memories'], [])

    def test_the_search_memory_tool_uses_recall(self):
        from sage import tools
        memory.save('The ROV tether uses RS485 between the surface and the vehicle')
        result = tools.execute('search_memory', {'query': 'ROV tether', 'include_history': True})
        self.assertEqual(result['memories'][0]['tier'], 'semantic')

    # ---------------------------------------------------------------- index freshness and the voice path

    def test_the_index_notices_changes_made_behind_its_back(self):
        memory.save('The printer is an Ender 3 with a direct drive upgrade')
        self.assertEqual(len(memory.relevant('Ender 3 direct drive upgrade printer', limit=3, semantic=False)), 1)
        db.execute("UPDATE memories SET status='archived',updated_at=?", (db.now(),))
        self.assertEqual(memory.relevant('Ender 3 direct drive upgrade printer', limit=3, semantic=False), [])

    def test_voice_retrieval_never_calls_the_embedding_model(self):
        memory.save('The workshop wall display runs on a Raspberry Pi 4 in kiosk mode')
        with patch('sage.memory._embed') as embed, patch('sage.memory.semantic_ready', return_value=True):
            from sage import agent
            history, memories = agent._relevant_context('what does the workshop wall display run on', limit=8, recent_limit=0, fast=True)
        embed.assert_not_called()
        self.assertEqual(len(memories), 1)


if __name__ == '__main__':
    unittest.main()
