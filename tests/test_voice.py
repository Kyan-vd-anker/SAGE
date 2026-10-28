import struct
import unittest

from sage import voice_util as v


class VoiceUtilTests(unittest.TestCase):
    def test_speech_text_strips_markdown_code_and_sources(self):
        text = "<think>hmm</think>## Plan\n- **Buy** the `parts`\n- See [docs](https://x.y/z)\n\n```python\nprint(1)\n```\n\nSources:\n- A: https://a.b"
        out = v.speech_text(text)
        self.assertNotIn('think', out)
        self.assertNotIn('```', out)
        self.assertNotIn('http', out)
        self.assertNotIn('Sources', out)
        self.assertNotIn('**', out)
        self.assertIn('Buy the parts.', out)
        self.assertTrue(out.endswith('I put the code in the chat.'))

    def test_speech_text_dutch_note_and_times(self):
        out = v.speech_text('Vergadering om 07:30\n```x```', lang='nl')
        self.assertIn('7:30', out)
        self.assertTrue(out.endswith('De code staat in de chat.'))

    def test_speech_text_truncates_on_sentence(self):
        text = ' '.join(f'Sentence number {i} is here.' for i in range(60))
        out = v.speech_text(text, max_chars=200)
        self.assertLess(len(out), 260)
        self.assertTrue(out.endswith('The rest is in the chat.'))
        self.assertIn('here. The rest', out)

    def test_split_sentences(self):
        chunks = v.split_sentences('One. Two! Three? ' + 'word ' * 100, limit=80)
        self.assertEqual(chunks[:3], ['One.', 'Two!', 'Three?'])
        self.assertTrue(all(len(c) <= 80 for c in chunks))

    def test_guess_lang(self):
        self.assertEqual(v.guess_lang('Ik heb een afspraak vandaag met de dokter'), 'nl')
        self.assertEqual(v.guess_lang('What is the weather like today'), 'en')
        self.assertEqual(v.guess_lang('Sage', default='nl'), 'nl')

    def test_noise_filter(self):
        self.assertTrue(v.looks_like_noise('Thanks for watching!'))
        self.assertTrue(v.looks_like_noise(' . '))
        self.assertFalse(v.looks_like_noise('set a timer for ten minutes'))

    def test_resample_length_and_identity(self):
        pcm = struct.pack('<' + 'h' * 22050, *[(i * 7) % 2000 - 1000 for i in range(22050)])
        out = v.resample(pcm, 22050, 16000)
        self.assertAlmostEqual(len(out) / 2, 16000, delta=2)
        self.assertEqual(v.resample(pcm, 16000, 16000), pcm)

    def test_resample_without_numpy_matches_length(self):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == 'numpy':
                raise ImportError
            return real(name, *a, **k)
        builtins.__import__ = fake
        try:
            pcm = struct.pack('<' + 'h' * 8000, *range(-4000, 4000))
            out = v.resample(pcm, 8000, 16000)
        finally:
            builtins.__import__ = real
        self.assertAlmostEqual(len(out) / 2, 16000, delta=2)

    def test_wav_roundtrip_and_stereo_mixdown(self):
        pcm = struct.pack('<hhhh', 100, 300, -200, -400)
        self.assertEqual(v.parse_audio(v.wav_bytes(pcm)), pcm)
        self.assertEqual(v.to_16k_mono(pcm, 16000, 2), struct.pack('<hh', 200, -300))

    def test_raw_pcm_passthrough_and_odd_byte(self):
        self.assertEqual(v.parse_audio(b'\x01\x00\x02\x00\x03'), b'\x01\x00\x02\x00')

    def test_rms(self):
        self.assertEqual(v.rms(b'\x00\x00' * 100), 0)
        self.assertAlmostEqual(v.rms(struct.pack('<hh', 1000, -1000)), 1000)

    def test_intents(self):
        cases = {
            'Good night': 'sleep', 'goodnight sage': 'sleep', 'Welterusten': 'sleep', 'sleep mode': 'sleep',
            'Good morning': 'wake', 'Goedemorgen': 'wake', "I'm up": 'wake',
            'What needs my attention?': 'needs', 'give me a status update': 'needs',
            'What timers do I have?': 'timers', 'how long is left': 'timers', 'welke timers heb ik': 'timers',
            'what did you do while I was away': 'recap', 'wat is er gebeurd': 'recap',
            'when was the last backup': 'backup', 'what has the speaker heard': '',
            'show me the voice log': 'audit', 'who has talked to you': 'audit',
            'set a timer for ten minutes': '', 'remind me to call the dentist': '',
            'tell me a story about a good night for fishing and a long river trip across the country': '',
        }
        for text, expected in cases.items():
            self.assertEqual(v.match_intent(text), expected, text)

    def test_sign_is_stable_and_sensitive(self):
        a = v.sign('secret', 1000, 'POST', '/api/voice/ask', b'audio')
        self.assertEqual(a, v.sign('secret', 1000, 'POST', '/api/voice/ask', b'audio'))
        self.assertEqual(len(a), 64)
        for changed in (v.sign('other', 1000, 'POST', '/api/voice/ask', b'audio'), v.sign('secret', 1001, 'POST', '/api/voice/ask', b'audio'),
                        v.sign('secret', 1000, 'GET', '/api/voice/ask', b'audio'), v.sign('secret', 1000, 'POST', '/api/voice/poll', b'audio'),
                        v.sign('secret', 1000, 'POST', '/api/voice/ask', b'audio2')):
            self.assertNotEqual(a, changed)


class TrustTests(unittest.TestCase):
    def test_voice_gets_less_power_and_it_is_logged(self):
        from sage import trust
        self.assertFalse(trust.voice_blocks('save_design_version'))          # keyboard: not blocked here
        with trust.source('voice'):
            self.assertTrue(trust.is_voice())
            self.assertFalse(trust.voice_blocks('start_timer'))
            self.assertTrue(trust.voice_blocks('create_project'))
            self.assertTrue(trust.voice_blocks('schedule_task'))
            self.assertTrue(trust.voice_blocks('forget_memory'))
            self.assertTrue(trust.voice_blocks('some_new_plugin'))
            self.assertEqual(trust.drain_blocked(), ['create_project', 'schedule_task', 'forget_memory', 'some_new_plugin'])
        self.assertFalse(trust.is_voice())
        self.assertEqual(trust.drain_blocked(), [])

    def test_source_nests_and_restores(self):
        from sage import trust
        with trust.source('voice'):
            with trust.source('web'):
                self.assertFalse(trust.is_voice())
            self.assertTrue(trust.is_voice())


if __name__ == '__main__':
    unittest.main()
