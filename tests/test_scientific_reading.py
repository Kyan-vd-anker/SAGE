import io
import re
import unittest
from unittest.mock import patch

from sage import config, docqa, pdftext, scidoc, scisummary
try:
    from tests import docqa_fixtures as fx
    from tests import sci_fixtures as sf
except ImportError:          # run from inside the tests folder
    import docqa_fixtures as fx
    import sci_fixtures as sf

docqa._LOG_READY[0] = True


def have_pypdf():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


class FakeModel:
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, messages, max_tokens):
        self.calls.append(messages)
        reply = self.script(messages, max_tokens) if callable(self.script) else self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return reply if isinstance(reply, dict) else {'role': 'assistant', 'content': reply}

    def prompts(self):
        return ['\n'.join(m['content'] for m in call) for call in self.calls]


def science_model(messages, max_tokens):
    """A faithful model: per-section notes copied from the passages (plus one defined term), then a synthesis of the notes."""
    user = messages[-1]['content']
    passages = re.findall(r'(?m)^\[(D\d+) (?:p\.|part )(\d+)\] (.+)$', user)
    if 'TASK: List the facts' in user:
        tags = ['finding|prior', 'mechanism|prior', 'compare|prior']
        lines = []
        for number, (label, page, text) in enumerate(passages[:3]):
            lines.append(f"- [{label} p.{page}] [{tags[number % 3]}] {re.split(r'(?<=[.!?])\s', text)[0][:200]}")
            term = re.match(r'^(.*?) \(\w+\) are formed by', text)
            if term:
                lines.append(f"TERM [{label} p.{page}] {term.group(1).split('. ')[-1]} :: {re.split(r'(?<=[.!?])\s', text)[0][:200]}")
        return '\n'.join(lines)
    notes = re.findall(r'(?m)^- \[(D\d+) p\.(\d+)\] \((\w+)(?:\|\w+)?\) (.+)$', user)
    terms_ = re.findall(r'(?m)^TERM \[(D\d+) p\.(\d+)\] (.+?) :: (.+)$', user)
    out = ['## Short overview']
    for label, page, _, fact in notes[:2]:
        out.append(f'{fact} [{label} p.{page}]')
    if len(notes) >= 2:
        (l1, p1, _, f1), (l2, p2, _, f2) = notes[0], notes[1]
        out.append(f"Interpretation: {' '.join(f1.split()[:6])} and {' '.join(f2.split()[:6])} are discussed together [{l1} p.{p1}] [{l2} p.{p2}].")
    out.append('## Key findings')
    for label, page, _, fact in notes[2:5]:
        out.append(f'{fact} [{label} p.{page}]')
    out.append('## Important terminology')
    for label, page, name, definition in terms_:
        out.append(f'{name}: {definition} [{label} p.{page}]')
    return '\n'.join(out)


@unittest.skipUnless(have_pypdf(), 'pypdf not installed')
class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = sf.journal_review_pdf()
        cls.pages, cls.meta = pdftext.extract(cls.raw)

    def test_the_two_column_pages_are_read_column_by_column(self):
        for number, tag in ((2, 'PageTwo'), (3, 'PageThree'), (4, 'PageFour')):
            text = self.pages[number - 1]
            self.assertLess(text.index(f'zzLeftEnd{tag}zz'), text.index(f'zzRightStart{tag}zz'), f'page {number} columns are interleaved')
        self.assertEqual(self.meta['columns'], [2, 3, 4])

    def test_the_fixture_really_is_interleaved_by_plain_extraction(self):
        from pypdf import PdfReader
        plain = PdfReader(io.BytesIO(self.raw)).pages[1].extract_text()
        self.assertGreater(plain.index('zzLeftEndPageTwozz'), plain.index('zzRightStartPageTwozz'))

    def test_sentences_are_not_broken_across_the_columns(self):
        text = ' '.join(self.pages[1].split())
        self.assertIn('Liquid crystal networks (LCNs) are formed by photopolymerisation of reactive mesogens such as RM257', text)

    def test_headings_and_large_text_are_remembered_for_structure(self):
        hints = set(self.meta['headings'])
        for heading in ('1. introduction', '2. liquid crystal oligomer networks', '3. comparison', 'limitations', 'abstract'):
            self.assertIn(heading, hints)
        self.assertNotIn(sf.AUTHORS.lower(), hints)                  # the author line is front matter, not a heading

    def test_first_page_lines_carry_font_sizes(self):
        sizes = {b['text']: b['size'] for b in self.meta['blocks']}
        self.assertGreater(sizes[sf.TITLE_1], sizes[sf.JOURNAL])
        self.assertGreater(sizes[sf.JOURNAL], sizes['www.afm-journal.de'])


@unittest.skipUnless(have_pypdf(), 'pypdf not installed')
class IdentificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = docqa.load_document('journal-review.pdf', sf.journal_review_pdf())

    def test_the_journal_banner_is_not_mistaken_for_the_title(self):
        meta = self.doc.meta
        self.assertEqual(meta['title'], sf.TITLE)
        self.assertEqual(meta['journal'], sf.JOURNAL)
        self.assertNotEqual(meta['title'], meta['journal'])

    def test_authors_year_doi_and_type_are_extracted(self):
        meta = self.doc.meta
        self.assertEqual(meta['authors'], sf.AUTHORS)                # the Title Case title is not taken for a list of names
        self.assertEqual(meta['year'], 2025)
        self.assertEqual(meta['doi'], sf.DOI)
        self.assertEqual(meta['type'], 'review article')

    def test_the_identification_block_shows_journal_and_doi(self):
        block = docqa.identification_block(self.doc)
        for expected in (sf.TITLE, sf.AUTHORS, f'Journal: {sf.JOURNAL}', f'DOI: {sf.DOI}', 'Year: 2025'):
            self.assertIn(expected, block)

    def test_running_headers_and_page_numbers_are_removed_but_page_one_keeps_its_banner(self):
        self.assertIn(sf.JOURNAL, self.doc.headers)
        self.assertNotIn(sf.JOURNAL, self.doc.pages[1])
        self.assertNotRegex(self.doc.pages[1], r'(?m)^\s*2\s*$')
        self.assertIn(sf.JOURNAL, self.doc.pages[0])

    def test_without_font_information_the_banner_is_still_skipped(self):
        lines = [sf.JOURNAL, 'www.afm-journal.de', 'REVIEW', sf.TITLE_1, sf.TITLE_2, sf.AUTHORS, f'DOI: {sf.DOI}',
                 'Received: 4 February 2025; Published online: 20 May 2025', 'Abstract', sf.ABSTRACT]
        doc = docqa.build_document('flat.txt', ['\n'.join(lines)], 'part ')
        self.assertEqual(doc.meta['title'], sf.TITLE)
        self.assertEqual(doc.meta['journal'], sf.JOURNAL)
        self.assertEqual(doc.meta['doi'], sf.DOI)


@unittest.skipUnless(have_pypdf(), 'pypdf not installed')
class StructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = docqa.load_document('journal-review.pdf', sf.journal_review_pdf())

    def test_sections_follow_the_papers_own_headings_with_roles(self):
        found = {s['heading']: s for s in self.doc.structure['sections']}
        for heading, role in (('Abstract', 'abstract'), ('Introduction', 'introduction'), ('Liquid Crystal Oligomer Networks', 'topic'),
                              ('Comparison', 'topic'), ('Limitations', '')):
            self.assertIn(heading, found)
        self.assertEqual(found['Abstract']['role'], 'abstract')
        self.assertEqual(found['Introduction']['role'], 'introduction')
        self.assertEqual(found['Comparison']['pages'][0], 4)

    def test_captions_tables_and_equations_are_extracted(self):
        captions = {c['label']: c for c in self.doc.structure['captions']}
        self.assertEqual(captions['Table 1']['page'], 3)
        self.assertIn('Properties of LCN and LCON films', captions['Table 1']['text'])
        self.assertEqual(captions['Figure 2']['kind'], 'figure')
        rows = self.doc.structure['tables'][0]['rows']
        self.assertIn('LCN 85 MPa 45 \u00b0C 12 %', rows)
        self.assertIn('LCON 1.2 MPa 18 \u00b0C 55 %', rows)
        self.assertTrue(any('(1)' in e['text'] for e in self.doc.structure['equations']))

    def test_table_values_can_be_retrieved_for_questions(self):
        hits, info = docqa.retrieve(docqa.assign_labels([self.doc]), 'What modulus does the LCON film have?')
        self.assertTrue(info['answerable'])
        self.assertIn('1.2 MPa', ' '.join(h.chunk.text for h in hits))

    def test_the_reference_list_is_still_excluded(self):
        self.assertGreaterEqual(self.doc.ref_start_page, 5)
        self.assertTrue(all('Warner' not in c.text for c in self.doc.body_chunks()))


class QualityTests(unittest.TestCase):
    def test_scanned_documents_are_rejected_with_an_ocr_hint(self):
        if not have_pypdf():
            self.skipTest('pypdf not installed')
        with self.assertRaises(ValueError) as raised:
            docqa.load_document('scan.pdf', sf.scanned_pdf())
        self.assertIn('OCR', str(raised.exception))

    def test_a_page_without_text_is_reported_not_ignored(self):
        if not have_pypdf():
            self.skipTest('pypdf not installed')
        doc = docqa.load_document('partial.pdf', sf.partly_empty_pdf())
        self.assertTrue(any('page 4' in w for w in doc.warnings), doc.warnings)
        answer = docqa.respond('Summarize this paper.', [doc], FakeModel(science_model), num_ctx=4096, answer_tokens=1000)
        self.assertIn('No text could be extracted from page 4', answer.text)

    def test_garbled_text_is_detected(self):
        report = pdftext.quality_report(['\ufffd\ufffd x y z w ' * 40, 'Normal readable scientific prose about liquid crystal networks. ' * 10,
                                         'More normal readable scientific prose about actuator materials. ' * 10])
        self.assertEqual(report['garbled_pages'], [1])
        self.assertFalse(report['severe'])
        self.assertIn('page 1 looks garbled', report['warnings'][0])
        severe = pdftext.quality_report(['\ufffd\ufffd x y z w ' * 40] * 3)
        self.assertTrue(severe['severe'])

    def test_extraction_artefacts_are_repaired_without_touching_science(self):
        text = pdftext.repair_text('The \ufb01rst \ufb02ow of L i q u i d  C r y s t a l samples at 25 \u00b0C: TiO2, 1.2 \u00d7 10\u22123 S/m, \u03c3 = E\u03b5 (1)')
        self.assertIn('first flow', text)
        self.assertIn('Liquid', text)
        for kept in ('25 \u00b0C', 'TiO2', '1.2 \u00d7 10\u22123 S/m', '\u03c3 = E\u03b5 (1)'):
            self.assertIn(kept, text)


@unittest.skipUnless(have_pypdf(), 'pypdf not installed')
class SectionAwareSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = docqa.load_document('journal-review.pdf', sf.journal_review_pdf())

    def summarize(self, model=None, **kwargs):
        model = model or FakeModel(science_model)
        answer = docqa.respond('Summarize this paper.', [self.doc], model, num_ctx=kwargs.pop('num_ctx', 2048), answer_tokens=kwargs.pop('answer_tokens', 600))
        return answer, model

    def test_the_paper_is_read_section_by_section_with_a_question_that_fits_each_section(self):
        answer, model = self.summarize()
        maps = [p for p in model.prompts() if 'TASK: List the facts' in p]
        self.assertGreaterEqual(len(maps), 3)
        sections = [re.search(r'SECTION: (.+?);', p).group(1) for p in maps]
        self.assertTrue(any('Abstract' in s or 'Front matter' in s for s in sections))
        self.assertTrue(any('Introduction' in s for s in sections))
        for prompt in maps:
            self.assertIn('focus on:', prompt)
        intro = next(p for p in maps if 'Soft actuators change shape' in p)
        later = next(p for p in maps if 'Michael addition' in p)
        self.assertNotIn('Michael addition', intro)                                # the introduction is not mixed into the later sections
        self.assertNotIn('Soft actuators change shape', later)
        self.assertIn('which approach, material or technology', ' '.join(maps))   # review topic sections get the topic question
        self.assertIn('the objective, the approach and the main message', ' '.join(maps))

    def test_the_synthesis_follows_the_requested_structure_and_marks_interpretation(self):
        answer, model = self.summarize()
        final = [p for p in model.prompts() if 'VERIFIED NOTES' in p][-1]
        for heading in ('## Short overview', '## Scientific background', '## Main scientific concepts', '## Approaches and technologies covered',
                        '## Key findings reported', '## Comparisons', '## Limitations and challenges', '## Conclusions and future directions',
                        '## Important terminology'):
            self.assertIn(heading, final)
        self.assertIn('Interpretation:', final)
        self.assertIn('== ', final)                                                 # notes arrive grouped by section
        self.assertEqual(answer.status, 'verified', answer.report)
        self.assertIn('Interpretation:', answer.text)
        self.assertIn('Reading guide', answer.text)
        self.assertGreaterEqual(answer.report['documents'][0]['notes'], 3)

    def test_defined_terms_are_collected_verified_and_offered_for_the_terminology_section(self):
        answer, model = self.summarize()
        final = [p for p in model.prompts() if 'VERIFIED NOTES' in p][-1]
        self.assertIn('TERMS (verified definitions)', final)
        self.assertRegex(final, r'TERM \[D1 p\.\d+\] Liquid crystal networks :: ')
        self.assertIn('Important terminology', answer.text)

    def test_the_output_identifies_the_paper_and_lists_its_figures_and_tables(self):
        answer, _ = self.summarize()
        for expected in (sf.TITLE, f'Journal: {sf.JOURNAL}', f'DOI: {sf.DOI}', 'Figures and tables in the paper', 'Table 1 (p.3)', 'Figure 2 (p.2)'):
            self.assertIn(expected, answer.text)

    def test_an_invented_note_is_dropped_before_it_can_reach_the_summary(self):
        def lying_model(messages, max_tokens):
            user = messages[-1]['content']
            if 'TASK: List the facts' in user:
                page = re.search(r'(?m)^\[D1 p\.(\d+)\]', user).group(1)
                return f'- [D1 p.{page}] [finding|own] The LCON films reached a modulus of 999 MPa with PANI fibres.\n' + science_model(messages, max_tokens)
            return science_model(messages, max_tokens)
        answer, model = self.summarize(FakeModel(lying_model))
        self.assertGreater(answer.report['documents'][0]['notes_dropped_unverified'], 0)
        self.assertNotIn('999', answer.text)
        self.assertNotIn('PANI', ' '.join(p for p in model.prompts() if 'VERIFIED NOTES' in p))

    def test_not_analysed_sections_are_named_when_the_paper_is_too_long_for_the_budget(self):
        with patch.object(config, 'DOC_MAP_MAX_CALLS', 2):
            answer, _ = self.summarize()
        self.assertIn('Coverage:', answer.text)
        self.assertIn('Not analysed:', answer.text)
        self.assertTrue(answer.report['documents'][0]['pages_not_analysed'])

    def test_the_most_informative_sections_are_kept_when_some_must_be_dropped(self):
        units, skipped = scisummary.plan_units(self.doc, 700, 3)
        self.assertEqual(len(units), 3)
        kept = {u['role'] for u in units}
        self.assertTrue(kept & {'abstract', 'topic', 'results', 'conclusion'})
        self.assertTrue(skipped)

    def test_if_the_section_reader_fails_the_simple_reader_still_produces_a_summary(self):
        with patch('sage.scisummary.summarize_one', side_effect=ValueError('boom')):
            answer, _ = self.summarize()
        self.assertIn('Document identification', answer.text)
        self.assertNotEqual(answer.status, 'error')


class InterpretationAndEntityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pages = [' \n'.join(p) for p in fx.lcp_review_pages()]
        cls.review = docqa.build_document('review.pdf', pages)
        epoxy = [' \n'.join(p) for p in fx.graphene_epoxy_pages()]
        cls.epoxy = docqa.build_document('epoxy.pdf', epoxy)

    def docs(self, *docs):
        return docqa.assign_labels(list(docs))

    def page(self, doc, phrase):
        for number, text in enumerate(doc.body_pages, 1):
            if phrase in ' '.join(text.split()):
                return number
        raise AssertionError(phrase)

    def test_an_interpretation_must_cite_pages_and_is_counted(self):
        page = self.page(self.epoxy, '1.84')
        docs = self.docs(self.epoxy)
        good = f'Interpretation: thermal conductivity rose and tensile strength fell with more filler [D1 p.{page}].'
        result = docqa.verify_answer(good, docs)
        self.assertEqual((result.flagged, result.interpretations), ([], 1))
        uncited = docqa.verify_answer('Interpretation: thermal conductivity rose and tensile strength fell with more filler.', docs)
        self.assertIn('must cite', uncited.flagged[0][1])

    def test_an_interpretation_that_goes_beyond_its_pages_is_still_flagged(self):
        page = self.page(self.epoxy, '1.84')
        result = docqa.verify_answer(f'Interpretation: this proves graphene will replace copper in every heat sink design [D1 p.{page}].', self.docs(self.epoxy))
        self.assertEqual(len(result.flagged), 1)

    def test_a_material_that_is_not_in_the_paper_is_caught(self):
        page = self.page(self.review, 'nematic to isotropic')
        result = docqa.verify_answer(f'Acrylate networks described in earlier studies show a nematic to isotropic transition temperature of PANI [D1 p.{page}].',
                                     self.docs(self.review))
        self.assertEqual(len(result.flagged), 1)
        self.assertIn('PANI', result.flagged[0][1])

    def test_two_materials_from_different_places_are_not_mixed_up(self):
        doc = docqa.build_document('two.pdf', [
            'Intro text about the work.\nLCNs are stiff polymer films made by photopolymerisation of RM257 with a high crosslink density and a sharp response.\n' * 3,
            'Oligomer text.\nLCONs are soft films made by Michael addition of dithiols to diacrylate mesogens with long flexible segments and low modulus.\n' * 3,
        ])
        docs = self.docs(doc)
        mixed = docqa.verify_answer('LCONs and RM257 are made by photopolymerisation with a high crosslink density [D1 p.1].', docs)
        self.assertEqual(len(mixed.flagged), 1)
        self.assertIn('mixes entities', mixed.flagged[0][1])
        right = docqa.verify_answer('LCNs are stiff polymer films made by photopolymerisation of RM257 with a high crosslink density [D1 p.1].', docs)
        self.assertEqual(right.flagged, [])

    def test_units_are_not_mistaken_for_materials(self):
        page = self.page(self.epoxy, '1.84')
        result = docqa.verify_answer(f'Tensile strength fell from 62 MPa to 48 MPa [D1 p.{page}].', self.docs(self.epoxy))
        self.assertEqual(result.flagged, [])


class RoleTests(unittest.TestCase):
    def test_section_roles(self):
        for heading, role in (('Abstract', 'abstract'), ('Introduction', 'introduction'), ('Materials and Methods', 'methods'), ('Results and Discussion', 'results'),
                              ('Conclusions', 'conclusion'), ('Challenges and Outlook', 'conclusion'), ('Acknowledgements', 'backmatter'),
                              ('Liquid Crystal Oligomer Networks', '')):
            self.assertEqual(scidoc.role(heading), role, heading)

    def test_dois_and_journal_like_lines(self):
        self.assertEqual(scidoc.find_doi('see https://doi.org/10.1021/acs.macromol.5b00123.'), '10.1021/acs.macromol.5b00123')
        self.assertTrue(scidoc.journal_like('Advanced Functional Materials'))
        self.assertTrue(scidoc.journal_like('Journal of Applied Physics'))
        self.assertFalse(scidoc.journal_like('Unlocking the world of material intelligence with liquid crystal polymers'))


if __name__ == '__main__':
    unittest.main()
