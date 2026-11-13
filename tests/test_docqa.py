import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sage import agent, config, db, docqa, llm
try:
    from tests import docqa_fixtures as fx
except ImportError:          # run from inside the tests folder
    import docqa_fixtures as fx

docqa._LOG_READY[0] = True   # tests must not open a log file inside a temporary folder


def page_of(doc, phrase):
    for number, text in enumerate(doc.body_pages, 1):
        if phrase in ' '.join(text.split()):
            return number
    raise AssertionError(f'{phrase!r} not found in the body of {doc.name}')


def build(name, pages):
    return docqa.build_document(name, [' \n'.join(lines) for lines in pages])


class FakeModel:
    """Scripted stand-in for Ollama. `script` is a list of replies or a function(messages, max_tokens) -> reply."""
    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        if callable(self.script):
            reply = self.script(messages, max_tokens)
        else:
            reply = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return reply if isinstance(reply, dict) else {'role': 'assistant', 'content': reply}

    def prompts(self):
        return ['\n'.join(m['content'] for m in call) for call in self.calls]


def summarizer(messages, max_tokens):
    """Behaves like a faithful model for both the note-taking passes and the final write-up."""
    user = messages[-1]['content']
    passages = re.findall(r'(?m)^\[(D\d+) (?:p\.|part )(\d+)\] (.+)$', user)
    if 'TASK: List the facts' in user:
        lines = []
        for label, page, text in passages[:4]:
            sentence = re.split(r'(?<=[.!?])\s', text)[0][:170]
            lines.append(f'- [{label} p.{page}] [own] {sentence}')
        return '\n'.join(lines)
    notes = re.findall(r'(?m)^- \[(D\d+) p\.(\d+)\] \((\w+)(?:\|\w+)?\) (.+)$', user)
    if notes:
        return '\n'.join(f'{fact} [{label} p.{page}]' for label, page, _, fact in notes[:6])
    return '\n'.join(f'{re.split(r"(?<=[.!?])\s", text)[0]} [{label} p.{page}]' for label, page, text in passages[:5])


class Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lcp = build('lcp-review.pdf', fx.lcp_review_pages())
        cls.epoxy = build('graphene-epoxy.pdf', fx.graphene_epoxy_pages())
        cls.display = build('lc-display.pdf', fx.lc_display_pages())

    def labelled(self, *docs):
        return docqa.assign_labels(list(docs))


class IngestionTests(Corpus):
    def test_pdf_ingestion_keeps_pages_and_separates_the_reference_list(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest('pypdf not installed')
        pages = fx.lcp_review_pages()
        doc = docqa.load_document('review.pdf', fx.pdf_bytes(pages))
        self.assertEqual(doc.n_pages, len(pages))
        self.assertGreaterEqual(doc.ref_start_page, len(pages) - 1)
        self.assertTrue(any(c.is_reference and 'Gelebart' in c.text for c in doc.chunks))
        for chunk in doc.body_chunks():
            self.assertNotIn('Gelebart', chunk.text)
            self.assertNotIn('PANI', chunk.text)
            self.assertTrue(1 <= chunk.page <= doc.n_pages)
        self.assertEqual(doc.meta['title'], fx.LCP_TITLE)

    def test_identifies_a_review_article_from_its_own_front_matter(self):
        meta = self.lcp.meta
        self.assertEqual(meta['title'], fx.LCP_TITLE)
        self.assertEqual(meta['authors'], fx.LCP_AUTHORS)
        self.assertEqual(meta['year'], 2025)
        self.assertEqual(meta['type'], 'review article')
        self.assertIn('liquid', meta['subject'].lower())

    def test_identifies_experimental_articles(self):
        self.assertEqual(self.epoxy.meta['type'], 'experimental research article')
        self.assertEqual(self.epoxy.meta['title'], 'Thermal conductivity of graphene-filled epoxy composites')
        self.assertEqual(self.epoxy.meta['year'], 2024)
        self.assertEqual(self.display.meta['type'], 'experimental research article')

    def test_document_ids_are_content_hashes_and_cache_never_crosses_documents(self):
        raw_a, raw_b = fx.pdf_bytes(fx.graphene_epoxy_pages()), fx.pdf_bytes(fx.lc_display_pages())
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest('pypdf not installed')
        a1, a2 = docqa.load_document('a.pdf', raw_a), docqa.load_document('renamed.pdf', raw_a)
        b = docqa.load_document('b.pdf', raw_b)
        self.assertEqual(a1.doc_id, a2.doc_id)
        self.assertEqual(a2.name, 'renamed.pdf')
        self.assertNotEqual(a1.doc_id, b.doc_id)
        self.assertIn('graphene', ' '.join(a2.pages).lower())
        self.assertNotIn('graphene', ' '.join(b.pages).lower())

    def test_missing_front_matter_is_reported_not_guessed(self):
        doc = docqa.build_document('notes.txt', ['Some loose notes about nothing in particular and how things went last week in the lab.'], 'part ')
        self.assertIsNone(doc.meta['title'])
        self.assertIsNone(doc.meta['authors'])
        self.assertIsNone(doc.meta['year'])
        self.assertIn('not found in the document', docqa.identification_block(doc))

    def test_scanned_or_empty_documents_are_rejected(self):
        with self.assertRaises(docqa.DocumentError):
            docqa.build_document('empty.pdf', ['', ' '])


class RetrievalTests(Corpus):
    def test_retrieval_returns_the_passage_that_answers_the_question(self):
        docs = self.labelled(self.epoxy)
        hits, info = docqa.retrieve(docs, 'What thermal conductivity was measured at 20 wt% loading?')
        self.assertTrue(info['answerable'])
        self.assertIn('1.84', ' '.join(h.chunk.text for h in hits))
        self.assertIn(page_of(self.epoxy, '1.84'), [h.chunk.page for h in hits])

    def test_the_reference_list_is_never_retrieved(self):
        docs = self.labelled(self.lcp)
        hits, info = docqa.retrieve(docs, 'What does the cilia fiber array paper by Gelebart find?')
        self.assertFalse(info['answerable'])
        self.assertTrue(all(not h.chunk.is_reference for h in hits))

    def test_a_question_the_document_cannot_answer_says_so_without_calling_the_model(self):
        model = FakeModel(['should never be used'])
        answer = docqa.respond('What is the boiling point of ethanol?', [self.epoxy], model)
        self.assertEqual(answer.status, 'not_found')
        self.assertEqual(model.calls, [])
        self.assertIn("can't find", answer.text)
        self.assertIn('ethanol', answer.text)

    def test_similar_terminology_in_two_documents_is_kept_apart(self):
        docs = self.labelled(self.lcp, self.display)           # both are about liquid crystals and alignment
        hits, _ = docqa.retrieve(docs, 'What actuation strain has been reported in aligned networks?')
        self.assertTrue(hits)
        self.assertEqual({h.doc.label for h in hits}, {'D1'})
        hits, _ = docqa.retrieve(docs, 'What rise time was measured at a drive voltage of 5 V?')
        self.assertTrue(hits)
        self.assertEqual({h.doc.label for h in hits}, {'D2'})

    def test_retrieval_is_limited_to_the_selected_documents(self):
        hits, info = docqa.retrieve(self.labelled(self.epoxy), 'What rise time was measured at a drive voltage of 5 V?')
        self.assertFalse(info['answerable'])
        self.assertEqual(hits, [])

    def test_prompts_contain_only_selected_document_text(self):
        model = FakeModel(['The rise time decreased from 21 ms to 12 ms at a drive voltage of 5 V [D2 p.1].'])
        docqa.respond('What rise time was measured at a drive voltage of 5 V?', [self.epoxy, self.display], model)
        joined = '\n'.join(model.prompts())
        self.assertIn('rise time decreased', joined)
        self.assertNotIn('graphene', joined.lower())
        self.assertNotIn('epoxy', joined.lower())

    def test_citations_use_the_page_the_text_came_from(self):
        docs = self.labelled(self.lcp)
        hits, _ = docqa.retrieve(docs, 'Which actuation strain was reported in aligned networks?')
        text, _used = docqa.render_passages(hits, 6000)
        for label, page, body in re.findall(r'(?m)^\[(D\d+) p\.(\d+)\] (.+)$', text):
            self.assertIn(body[:60].strip(), ' '.join(docs[0].body_pages[int(page) - 1].split()))


class BudgetTests(Corpus):
    def test_the_original_failure_cannot_recur_the_whole_paper_no_longer_goes_into_one_small_window(self):
        old_prompt_text = '\n\n'.join(self.lcp.pages)
        self.assertGreater(docqa.est_tokens(old_prompt_text), 4096)           # what used to be pushed into a 4096-token window
        model = FakeModel(summarizer)
        answer = docqa.respond('Summarize this paper.', [self.lcp], model, num_ctx=4096, answer_tokens=1000)
        self.assertEqual(answer.status, 'verified', answer.report)
        for call in model.calls:
            size = docqa.est_tokens('\n'.join(m['content'] for m in call))
            self.assertLessEqual(size + 700, 4096, 'a prompt does not fit the context window')
        self.assertIn(fx.LCP_TITLE, answer.text)
        self.assertNotIn('cilia', answer.text.lower())
        self.assertNotIn('PANI', answer.text)

    def test_context_overflow_is_detected_and_the_call_is_repeated_with_a_smaller_prompt(self):
        sizes = []

        def script(messages, max_tokens):
            sizes.append(sum(len(m['content']) for m in messages))
            if len(sizes) == 1:
                return {'role': 'assistant', 'content': 'truncated nonsense', '_prompt_tokens': 4096}
            return f'Thermal conductivity rose from 0.21 W/mK for neat epoxy to 1.84 W/mK at 20 wt% loading [D1 p.{page_of(self.epoxy, "1.84")}].'
        answer = docqa.respond('What thermal conductivity was measured at 20 wt% loading?', [self.epoxy], FakeModel(script), num_ctx=4096)
        self.assertEqual(answer.status, 'verified')
        self.assertEqual(len(sizes), 2)
        self.assertLessEqual(sizes[1], sizes[0])

    def test_persistent_overflow_is_an_error_not_a_silent_answer(self):
        model = FakeModel(lambda m, t: {'role': 'assistant', 'content': 'x', '_prompt_tokens': 4096})
        with self.assertRaises(docqa.ContextOverflow):
            docqa.respond('What thermal conductivity was measured at 20 wt% loading?', [self.epoxy], model, num_ctx=4096)

    def test_llm_sends_num_ctx_and_reports_the_prompt_size(self):
        response = MagicMock()
        response.status = 200
        response.__iter__.return_value = [b'{"message":{"content":"ok"},"done":true,"prompt_eval_count":321}\n']
        connection = MagicMock(getresponse=lambda: response)
        with patch('sage.llm.http.client.HTTPConnection', return_value=connection):
            reply = llm.chat_stream([{'role': 'user', 'content': 'q'}], model='m', max_tokens=50, num_ctx=8192)
        body = json.loads(connection.request.call_args.kwargs['body'])
        self.assertEqual(body['options'], {'num_predict': 50, 'num_ctx': 8192})
        self.assertEqual(reply['_prompt_tokens'], 321)


class VerificationTests(Corpus):
    def setUp(self):
        self.review = self.labelled(self.lcp)
        self.epoxy_docs = self.labelled(self.epoxy)

    def cite(self, doc, phrase):
        return f'[D1 p.{page_of(doc, phrase)}]'

    def test_a_grounded_answer_passes(self):
        c1, c2 = self.cite(self.lcp, 'rubbed polyimide'), self.cite(self.lcp, 'The authors propose')
        answer = (f'The review names rubbed polyimide layers, magnetic fields and shear flow as alignment routes {c1}. '
                  f'The authors propose machine-learning-guided design and multi-stimuli networks for future research {c2}.')
        result = docqa.verify_answer(answer, self.review)
        self.assertEqual(result.flagged, [])
        self.assertFalse(result.mismatch)

    def test_a_summary_of_the_wrong_paper_is_detected(self):
        wrong = ('The paper presents a photoresponsive fiber array that mimics the collective motion of cilia for transport [D1 p.1]. '
                 'The fibers are made of PANI/PDA composites and bend under alternating light fields [D1 p.2]. '
                 'Their motion is sensitive to temperature, and the array transports particles [D1 p.3].')
        result = docqa.verify_answer(wrong, self.review)
        self.assertEqual(len(result.flagged), 3)
        self.assertTrue(result.mismatch)
        self.assertTrue(result.ref_only_terms)

    def test_a_wrong_summary_is_regenerated_and_then_refused_if_it_stays_wrong(self):
        wrong = ('The review presents a photoresponsive fiber array that mimics the collective motion of cilia [D1 p.1]. '
                 'The fibers are made of PANI/PDA composites that bend under alternating light fields [D1 p.2]. '
                 'The challenges named are temperature sensitivity of the cilia-like fibers [D1 p.4].')
        model = FakeModel([wrong])
        answer = docqa.respond('What challenges and limitations does the review name?', [self.lcp], model)
        self.assertEqual(answer.status, 'unverified')
        self.assertEqual(len(model.calls), 2)
        self.assertIn('do not support', model.prompts()[1])
        self.assertNotIn('PANI', answer.text)
        self.assertIn('not presenting', answer.text)

    def test_a_wrong_first_draft_is_replaced_by_a_verified_second_draft(self):
        page = page_of(self.lcp, 'Scaling up synthesis')
        wrong = 'The review is about PANI/PDA fibers that mimic cilia and respond to alternating light fields [D1 p.1].'
        right = (f'Scaling up synthesis, fatigue under repeated cycling, integration with electronics and standardised characterisation '
                 f'remain open challenges [D1 p.{page}].')
        model = FakeModel([wrong, right])
        answer = docqa.respond('What challenges and limitations does the review name?', [self.lcp], model)
        self.assertEqual(answer.status, 'verified')
        self.assertEqual(answer.report['attempts'], 2)
        self.assertNotIn('PANI', answer.text)

    def test_numbers_and_units_are_checked_against_the_cited_page(self):
        page = page_of(self.epoxy, '1.84')
        good = f'Thermal conductivity rose from 0.21 W/mK to 1.84 W/mK at 20 wt% loading [D1 p.{page}]. Tensile strength fell from 62 MPa to 48 MPa [D1 p.{page}].'
        self.assertEqual(docqa.verify_answer(good, self.epoxy_docs).flagged, [])
        wrong_number = f'Thermal conductivity rose to 2.84 W/mK at 20 wt% loading [D1 p.{page}].'
        flagged = docqa.verify_answer(wrong_number, self.epoxy_docs).flagged
        self.assertEqual(len(flagged), 1)
        self.assertIn('2.84', flagged[0][1])
        wrong_unit = f'Tensile strength fell from 62 GPa to 48 GPa [D1 p.{page}].'
        flagged = docqa.verify_answer(wrong_unit, self.epoxy_docs).flagged
        self.assertEqual(len(flagged), 1)
        self.assertIn('unit', flagged[0][1])

    def test_citations_must_point_to_the_right_document_and_page(self):
        both = self.labelled(self.epoxy, self.display)
        page = page_of(self.display, 'rise time decreased')
        ok = f'The rise time decreased from 21 ms to 12 ms at a drive voltage of 5 V [D2 p.{page}].'
        self.assertEqual(docqa.verify_answer(ok, both).flagged, [])
        wrong_doc = ok.replace('[D2', '[D1')
        reason = docqa.verify_answer(wrong_doc, both).flagged[0][1]
        self.assertIn('another document', reason)
        self.assertIn('D2', reason)
        beyond = ok.replace(f'p.{page}', 'p.99')
        self.assertIn('does not exist', docqa.verify_answer(beyond, both).flagged[0][1])
        unknown = ok.replace('[D2', '[D7')
        self.assertIn('unknown document', docqa.verify_answer(unknown, both).flagged[0][1])

    def test_a_review_is_not_presented_as_the_authors_experiment(self):
        page = page_of(self.lcp, '50 \u00b5m')
        wrong = f'The authors demonstrated printed filaments with diameters of 50 \u00b5m [D1 p.{page}].'
        right = f'Printed filaments with diameters of 50 \u00b5m have been reported [D1 p.{page}].'
        self.assertIn('review article', docqa.verify_answer(wrong, self.review).flagged[0][1])
        self.assertEqual(docqa.verify_answer(right, self.review).flagged, [])

    def test_admitting_that_information_is_missing_is_always_acceptable(self):
        result = docqa.verify_answer('The document does not state the synthesis temperature. The curing schedule is not mentioned.', self.epoxy_docs)
        self.assertEqual((result.claims, result.flagged), (0, []))

    def test_uncited_claims_must_still_exist_in_the_document(self):
        result = docqa.verify_answer('Graphene oxide dispersions were sonicated for six hours before mixing.', self.epoxy_docs)
        self.assertEqual(len(result.flagged), 1)

    def test_a_few_unsupported_sentences_are_removed_and_disclosed(self):
        page = page_of(self.epoxy, '1.84')
        answer = (f'Thermal conductivity rose from 0.21 W/mK to 1.84 W/mK at 20 wt% loading [D1 p.{page}]. '
                  f'Tensile strength fell from 62 MPa to 48 MPa at the same loading [D1 p.{page}]. '
                  f'The glass transition temperature was 118 \u00b0C for the neat resin [D1 p.{page}]. '
                  f'The 30 wt% samples contained visible agglomerates [D1 p.{page}]. '
                  f'Samples were annealed at 300 \u00b0C for 10 h before testing [D1 p.{page}].')
        outcome = docqa._finalize(answer, self.epoxy_docs, 1)
        self.assertEqual(outcome.status, 'qualified')
        self.assertNotIn('300', outcome.text)
        self.assertIn('1.84', outcome.text)
        self.assertIn('could not be verified', outcome.text)

    def test_a_document_with_numerical_findings_is_summarised_with_correct_values(self):
        answer = docqa.respond('Summarize this paper.', [self.epoxy], FakeModel(summarizer))
        self.assertEqual(answer.status, 'verified', answer.report)
        self.assertIn('1.84', answer.text)
        self.assertIn('Thermal conductivity of graphene-filled epoxy composites', answer.text)
        self.assertEqual(answer.report['documents'][0]['mode'], 'single-pass')


class SummaryTests(Corpus):
    def test_summary_starts_with_verified_identification_and_labels_a_review_as_one(self):
        model = FakeModel(summarizer)
        answer = docqa.respond('Please summarize this paper', [self.lcp], model)
        self.assertTrue(answer.text.startswith('**Document identification**'))
        for expected in (fx.LCP_TITLE, fx.LCP_AUTHORS, '2025', 'review article'):
            self.assertIn(expected, answer.text)
        self.assertIn('review article', model.prompts()[-1])

    def test_long_documents_are_read_in_windows_and_the_coverage_is_reported(self):
        with patch.object(config, 'DOC_MAP_MAX_CALLS', 2):
            answer = docqa.respond('Summarize this paper.', [self.lcp], FakeModel(summarizer), num_ctx=4096, answer_tokens=1000)
        self.assertIn('Coverage:', answer.text)
        self.assertIn('Not analysed:', answer.text)
        self.assertTrue(answer.report['documents'][0]['pages_not_analysed'])

    def test_two_documents_in_one_conversation_never_share_prompts(self):
        model = FakeModel(summarizer)
        answer = docqa.respond('Summarize these papers.', [self.epoxy, self.display], model)
        self.assertEqual(answer.status, 'verified', answer.report)
        for prompt in model.prompts():
            if 'DOCUMENT D1' in prompt:
                self.assertNotIn('rubbed polyimide', prompt)
                self.assertNotIn('DOCUMENT D2', prompt)
            if 'DOCUMENT D2' in prompt:
                self.assertNotIn('graphene', prompt.lower())
        self.assertIn('### D1: graphene-epoxy.pdf', answer.text)
        self.assertIn('### D2: lc-display.pdf', answer.text)

    def test_a_summary_is_always_returned_even_if_it_cannot_be_fact_checked(self):
        wrong = ('The paper presents a photoresponsive fiber array that mimics the collective motion of cilia [D1 p.1]. '
                 'The fibers are made of PANI/PDA composites and bend under alternating light fields [D1 p.2]. '
                 'Their motion is sensitive to temperature, and the array transports particles [D1 p.3].')
        answer = docqa.respond('Summarize this paper.', [self.lcp], FakeModel([wrong]))
        self.assertEqual(answer.status, 'unverified')
        self.assertIn(fx.LCP_TITLE, answer.text)
        self.assertIn('Could not be fully fact-checked', answer.text)
        self.assertIn('progress in liquid crystal polymers', answer.text)      # the document's own abstract is the fallback summary
        self.assertNotIn('PANI', answer.text)                                 # a summary of some other paper is never shown

    def test_a_partly_unverifiable_summary_is_shown_with_the_unsupported_sentences_marked(self):
        page = page_of(self.epoxy, '1.84')
        draft = (f'Thermal conductivity rose from 0.21 W/mK to 1.84 W/mK at 20 wt% loading [D1 p.{page}]. '
                 f'Tensile strength fell from 62 MPa to 48 MPa [D1 p.{page}]. '
                 f'Samples were annealed at 300 \u00b0C for 10 h before testing [D1 p.{page}]. '
                 f'The matrix was a polyurethane foam reinforced with carbon fibres [D1 p.{page}].')
        answer = docqa.respond('Summarize this paper.', [self.epoxy], FakeModel([draft]))
        self.assertEqual(answer.status, 'unverified')
        self.assertIn('Could not be fully fact-checked', answer.text)
        self.assertIn('1.84 W/mK', answer.text)
        self.assertIn('\u26a0 *unverified:* Samples were annealed', answer.text)
        self.assertIn('\u26a0 *unverified:* The matrix was a polyurethane foam', answer.text)
        self.assertNotIn('\u26a0 *unverified:* Thermal conductivity', answer.text)

    def test_identification_questions_are_answered_from_the_file_without_the_model(self):
        model = FakeModel(['unused'])
        answer = docqa.respond('Who wrote this paper?', [self.lcp], model)
        self.assertEqual(answer.status, 'identified')
        self.assertIn('Pengrong Lyu', answer.text)
        self.assertEqual(model.calls, [])

    def test_thinking_blocks_from_reasoning_models_are_not_verified_as_claims(self):
        page = page_of(self.epoxy, '1.84')
        reply = f'<think>maybe 99 MPa and PANI fibres?</think>Thermal conductivity rose from 0.21 W/mK to 1.84 W/mK [D1 p.{page}].'
        answer = docqa.respond('What thermal conductivity was measured at 20 wt% loading?', [self.epoxy], FakeModel([reply]))
        self.assertEqual(answer.status, 'verified')
        self.assertNotIn('think', answer.text.lower())


class AgentIntegrationTests(Corpus):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault', WORKSPACE=root / 'workspace',
                                 PLUGIN_DIR=Path(__file__).resolve().parents[1] / 'plugins')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def test_document_turns_use_no_history_tools_or_reflection_and_their_answers_stay_out_of_later_context(self):
        page = page_of(self.epoxy, '1.84')
        reply = {'content': f'Thermal conductivity rose from 0.21 W/mK to 1.84 W/mK at 20 wt% loading [D1 p.{page}].'}
        db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)',
                   ('assistant', 'Earlier I summarised a cilia fiber array with PANI/PDA fibers.', db.now()))
        with patch('sage.agent.llm.chat_stream', return_value=reply) as stream, \
                patch('sage.agent.tools.definitions_for_query', side_effect=AssertionError('tools must not run')), \
                patch('sage.agent._relevant_context', side_effect=AssertionError('history must not be used')), \
                patch('sage.jobs.create', side_effect=AssertionError('no reflection from document answers')):
            events = []
            answer = agent.respond('What thermal conductivity was measured at 20 wt% loading?', documents=[self.epoxy], emit=events.append)
        self.assertIn('1.84', answer)
        self.assertNotIn('cilia', '\n'.join(m['content'] for m in stream.call_args.args[0]))
        self.assertEqual(stream.call_args.kwargs['num_ctx'], config.DOC_NUM_CTX)
        saved = db.rows("SELECT content,source_docs FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(saved['source_docs'], self.epoxy.doc_id)
        self.assertTrue(any(e.get('type') == 'token' and '1.84' in e['text'] for e in events))
        # ...and that answer is not offered as "history" to a later, unrelated request
        later = agent._relevant_context('thermal conductivity of graphene epoxy composites', limit=5)
        self.assertNotIn('1.84', json.dumps(later, default=str))

    def test_legacy_plain_text_context_still_goes_through_retrieval(self):
        text = '\n\n'.join('\n'.join(lines) for lines in fx.graphene_epoxy_pages())
        with patch('sage.agent.llm.chat_stream', return_value={'content': 'The document does not state that.'}) as stream:
            answer = agent.respond('What is the boiling point of ethanol in this paper?', document_context=text)
        self.assertIn("can't find", answer)
        stream.assert_not_called()

    def test_server_turns_uploaded_files_into_documents(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest('pypdf not installed')
        from sage import server
        encode = lambda raw: base64.b64encode(raw).decode()
        items = [{'name': 'a.pdf', 'data_b64': encode(fx.pdf_bytes(fx.graphene_epoxy_pages()))},
                 {'name': 'b.txt', 'data_b64': encode(b'plain notes about nothing in particular for this test of the loader')},
                 {'name': 'ignored.exe', 'data_b64': encode(b'MZ')}]
        loaded = server.Handler._selected_document_context(None, items)
        self.assertEqual([d.name for d in loaded], ['a.pdf', 'b.txt'])
        self.assertEqual(loaded[0].meta['title'], 'Thermal conductivity of graphene-filled epoxy composites')
        with self.assertRaises(ValueError):
            server.Handler._selected_document_context(None, [items[0]] * 6)
        with self.assertRaises(ValueError):
            server.Handler._selected_document_context(None, [items[2]])


if __name__ == '__main__':
    unittest.main()
