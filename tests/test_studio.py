import base64
import io
import json
import os
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from sage import config, db, studio


def build_pdf(text):
    stream = f'BT /F1 12 Tf 10 50 Td ({text}) Tj ET'.encode()
    objects = [
        b'<</Type/Catalog/Pages 2 0 R>>',
        b'<</Type/Pages/Kids[3 0 R]/Count 1>>',
        b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>',
        b'<</Length %d>>\nstream\n' % len(stream) + stream + b'\nendstream',
        b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>',
    ]
    out = bytearray(b'%PDF-1.4\n'); offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out)); out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for offset in offsets: out += b'%010d 00000 n \n' % offset
    out += b'trailer\n<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
    return bytes(out)


class StudioStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault', WORKSPACE=root / 'workspace')
        patcher.start(); self.addCleanup(patcher.stop)

    def test_create_save_list_and_delete(self):
        doc = studio.create_doc('latex', 'Thesis')
        self.assertIn('\\documentclass', doc['content'])
        saved = studio.save_doc(doc['id'], title='Thesis v2', content='\\section{A}')
        self.assertEqual((saved['title'], saved['content']), ('Thesis v2', '\\section{A}'))
        self.assertEqual([d['title'] for d in studio.list_docs()], ['Thesis v2'])
        studio.delete_doc(doc['id'])
        self.assertEqual(studio.list_docs(), [])
        with self.assertRaises(ValueError): studio.get_doc(doc['id'])

    def test_rejects_bad_kind_and_bad_sheet(self):
        with self.assertRaises(ValueError): studio.create_doc('slides', 'x')
        with self.assertRaises(ValueError): studio.create_doc('sheet', 'x', 'not json')
        with self.assertRaises(ValueError): studio.create_doc('sheet', 'x', json.dumps({'cells': {'ZZZZ1': '1'}}))
        with self.assertRaises(ValueError): studio.create_doc('doc', 'x', 'a' * 900_001)

    def test_routes_round_trip(self):
        handled, doc = studio.route_post('/api/studio/doc', {'kind': 'doc', 'title': 'Notes'})
        self.assertTrue(handled)
        studio.route_post(f"/api/studio/doc/{doc['id']}", {'content': '# Hi\n\nText.'})
        self.assertEqual(studio.route_get(f"/api/studio/doc/{doc['id']}", None)[1]['content'], '# Hi\n\nText.')
        self.assertEqual(studio.route_get('/api/studio/docs', None)[1][0]['title'], 'Notes')
        self.assertEqual(studio.route_post('/api/studio/nothing', {}), (False, None))


class LatexChecksTests(unittest.TestCase):
    def messages(self, text):
        return [(n.get('line'), n['severity'], n['message']) for n in studio.lint_latex(text)]

    def test_finds_mismatched_and_stray_environments(self):
        found = self.messages('\\documentclass{article}\n\\begin{document}\n\\begin{itemize}\n\\item a\n\\end{enumerate}\n\\end{document}\n')
        self.assertTrue(any(line == 5 and sev == 'error' and 'itemize' in msg for line, sev, msg in found), found)
        found = self.messages('\\documentclass{article}\n\\begin{document}\nHi\n\\end{center}\n\\end{document}\n')
        self.assertTrue(any(line == 4 and 'no matching' in msg for line, _, msg in found), found)

    def test_missing_label_duplicate_label_and_missing_documentclass(self):
        found = self.messages('\\begin{document}\n\\label{a}\n\\label{a}\nSee \\ref{b}.\n\\end{document}\n')
        self.assertTrue(any(line == 3 and 'already used' in msg for line, _, msg in found), found)
        self.assertTrue(any(line == 4 and "'b'" in msg for line, _, msg in found), found)
        self.assertTrue(any(line == 1 and 'documentclass' in msg for line, _, msg in found), found)

    def test_underscore_and_dollar_checks_respect_math_and_code(self):
        text = ('\\documentclass{article}\n\\begin{document}\n'
                'The value x_max is large.\n'
                'Math is fine: $x_max^2$ and \\texttt{my_file} and \\url{a_b.org} and 50\\% and snake\\_case.\n'
                '\\begin{equation}\nE = m_0 c^2\n\\end{equation}\n'
                'A price of $5 is odd.\n'
                '\\end{document}\n')
        found = self.messages(text)
        self.assertEqual([line for line, _, msg in found if "'_'" in msg], [3])
        self.assertEqual([line for line, _, msg in found if 'Odd number' in msg], [8])

    def test_comments_are_ignored_and_open_environment_is_reported(self):
        found = self.messages('\\documentclass{article}\n\\begin{document}\n% \\begin{foo} x_y\n\\begin{itemize}\n\\item a\n')
        self.assertEqual([m for m in found if 'foo' in m[2] or "'_'" in m[2]], [])
        self.assertTrue(any(line == 4 and 'not closed yet' in msg for line, _, msg in found), found)


class DocumentChecksTests(unittest.TestCase):
    def test_prose_checks_and_replacements(self):
        notes = studio.lint_doc('# One\n\n### Three\n\nThis is is a test  with  spaces , right?\n')
        by_line = {(n['line'], n['message'][:8]): n for n in notes}
        self.assertTrue(any('jumps' in n['message'] and n['line'] == 3 for n in notes))
        repeated = [n for n in notes if n['message'].startswith('Repeated')][0]
        self.assertEqual(repeated['replacement'], 'This is a test  with  spaces , right?')
        self.assertTrue(any(n['message'] == 'Double space' and '  ' not in n['replacement'] for n in notes))
        self.assertTrue(any(n['message'] == 'Space before punctuation' for n in notes))

    def test_code_blocks_are_skipped_and_long_sentences_flagged(self):
        notes = studio.lint_doc('```\nthe the  x\n```\n' + ' '.join(['alpha beta'] * 25) + '.\n')
        self.assertEqual([n['message'][:13] for n in notes], ['Long sentence'])


class SheetChecksTests(unittest.TestCase):
    def sheet(self, **cells):
        return json.dumps({'cells': cells})

    def test_errors_from_computed_values(self):
        notes = studio.lint_sheet(self.sheet(A1='=1/0', B1='=FOO(1)'), {'A1': '#DIV/0!', 'B1': '#NAME?', 'C1': 5})
        self.assertEqual([(n['cell'], n['severity']) for n in notes], [('A1', 'error'), ('B1', 'error')])

    def test_text_in_numeric_column_decimal_comma_and_short_sum(self):
        sheet = self.sheet(A1='Mass', A2='10', A3='2O', A4='30', A5='40', B2='1,5', C1='=SUM(A2:A4)')
        notes = studio.lint_sheet(sheet)
        cells = {n['cell']: n for n in notes}
        self.assertIn('typo', cells['A3']['message'])
        self.assertEqual(cells['B2']['replacement'], '1.5')
        self.assertIn('A5', cells['C1']['message'])
        self.assertNotIn('A1', cells)  # a header above the numbers is fine


class ReviewTests(unittest.TestCase):
    def test_focus_keeps_global_notes_and_hides_old_local_notes(self):
        text = ('\\documentclass{article}\n\\begin{document}\n'
                'old x_y here\n'
                'See \\ref{nope}.\n'
                'new a_b here\n'
                '\\end{document}\n')
        result = studio.review('latex', text, {'from': 5, 'to': 5}, use_model=False)
        messages = [(n.get('line'), n['message'][:12]) for n in result['notes']]
        self.assertIn((5, "A '_' outsid"), messages)
        self.assertNotIn((3, "A '_' outsid"), messages)
        self.assertIn((4, "Reference 'n"), messages)

    def test_model_notes_are_validated_and_merged(self):
        reply = 'Sure!\n```json\n[{"line": 2, "severity": "warn", "message": "Unclear sentence.", "replacement": "Better line."},' \
                '{"line": 99, "severity": "error", "message": "Out of range"},' \
                '{"line": 2, "severity": "bogus", "message": "Bad severity"},' \
                '{"line": 2, "severity": "info", "message": "Multi", "replacement": "a\\nb"}]\n```'
        with patch.object(studio, '_ask_model', return_value=reply):
            result = studio.review('doc', 'Intro\nA confusing line here.\n', {'from': 1, 'to': 2})
        ai = [n for n in result['notes'] if n['source'] == 'ai']
        self.assertEqual([(n['line'], n['message']) for n in ai], [(2, 'Unclear sentence.'), (2, 'Multi')])
        self.assertEqual(ai[0]['replacement'], 'Better line.')
        self.assertNotIn('replacement', ai[1])

    def test_a_failing_model_still_returns_the_local_notes(self):
        with patch.object(studio, '_ask_model', side_effect=OSError('ollama down')):
            result = studio.review('doc', 'This is is wrong.\n', {'from': 1, 'to': 1})
        self.assertEqual(len(result['notes']), 1)

    def test_sheet_review_uses_changed_cells_only(self):
        reply = '[{"cell": "B2", "severity": "warn", "message": "Unit looks off."}, {"cell": "Z9", "severity": "warn", "message": "Not changed"}]'
        sheet = json.dumps({'cells': {'A1': 'Mass', 'B1': 'kg', 'B2': '=A2*1000'}})
        with patch.object(studio, '_ask_model', return_value=reply) as ask:
            result = studio.review('sheet', sheet, {'cells': ['B2']}, {'B2': 5})
        self.assertIn('B2*', ask.call_args.args[1])
        self.assertEqual([n['cell'] for n in result['notes']], ['B2'])


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        studio._last_spoken = 0.0
        studio._spoken_keys.clear()
        self.note = {'severity': 'error', 'message': 'Undefined control sequence.', 'line': 7}

    def speak(self, notes, mode, room='bedroom', online=True, asleep=False):
        from sage import presence, voice
        with patch.object(config, 'SPEAKER_ROOM', 'bedroom'), patch.object(voice, 'online', return_value=online), patch.object(voice, '_asleep', asleep), \
             patch.object(presence, 'where', return_value={'state': 'room', 'room': room}), patch.object(voice, 'announce') as announce:
            return studio.maybe_speak(notes, mode), announce

    def test_speaks_only_for_errors_in_the_speaker_room(self):
        spoke, announce = self.speak([self.note], 'errors')
        self.assertTrue(spoke)
        self.assertEqual(announce.call_args.args[0], 'Line 7: Undefined control sequence.')
        self.assertEqual(self.speak([self.note], 'off')[0], False)

    def test_stays_quiet_when_away_asleep_or_offline(self):
        self.assertFalse(self.speak([self.note], 'errors', room='engineering')[0])
        self.assertFalse(self.speak([self.note], 'errors', asleep=True)[0])
        self.assertFalse(self.speak([self.note], 'errors', online=False)[0])

    def test_warnings_need_the_warnings_mode_and_repeats_are_limited(self):
        warn = {'severity': 'warn', 'message': 'Heading jumps', 'line': 3}
        self.assertFalse(self.speak([warn], 'errors')[0])
        self.assertTrue(self.speak([warn], 'warnings')[0])
        other = {'severity': 'error', 'message': 'Another problem', 'cell': 'B2'}
        self.assertFalse(self.speak([other], 'errors')[0])  # too soon after the last spoken note
        studio._last_spoken = time.time() - 100
        self.assertTrue(self.speak([other], 'errors')[0])
        studio._last_spoken = time.time() - 100
        self.assertFalse(self.speak([other], 'errors')[0])  # the same note is not said twice


class OfficeFileTests(unittest.TestCase):
    def test_docx_round_trip_keeps_headings_bold_and_lists(self):
        text = '# Title\n\nSome **bold** and *italic* words.\n\n- first\n- second\n\n## Part\n\nPlain.\n'
        data = studio.export_docx(text)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        back = studio.import_docx(data)
        self.assertIn('# Title', back)
        self.assertIn('## Part', back)
        self.assertIn('Some **bold** and *italic* words.', back)
        self.assertIn('first', back)

    def test_xlsx_round_trip_keeps_numbers_text_and_formulas(self):
        sheet = json.dumps({'cells': {'A1': 'Name & more', 'B1': '12.5', 'B2': '=SUM(B1:B1)*2', 'C3': 'x<y'}})
        data = studio.export_xlsx(sheet)
        back = json.loads(studio.import_xlsx(data))['cells']
        self.assertEqual(back, {'A1': 'Name & more', 'B1': '12.5', 'B2': '=SUM(B1:B1)*2', 'C3': 'x<y'})
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn('<f>SUM(B1:B1)*2</f>', z.read('xl/worksheets/sheet1.xml').decode())

    def test_xlsx_import_reads_shared_strings(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Hello</t></si></sst>')
            z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>3</v></c></row></sheetData></worksheet>')
        self.assertEqual(json.loads(studio.import_xlsx(buffer.getvalue()))['cells'], {'A1': 'Hello', 'B1': '3'})

    def test_csv_import_detects_semicolons_and_exports_back(self):
        content = studio.import_csv('naam;waarde\nA;1,5\nB;2\n')
        self.assertEqual(json.loads(content)['cells'], {'A1': 'naam', 'B1': 'waarde', 'A2': 'A', 'B2': '1,5', 'A3': 'B', 'B3': '2'})
        self.assertEqual(studio.export_csv(content), 'naam,waarde\nA,"1,5"\nB,2\n')

    def test_unsafe_or_broken_files_are_refused(self):
        with self.assertRaises(ValueError): studio.import_docx(b'not a zip')
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, 'w') as z:
            z.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        with self.assertRaises(ValueError): studio.import_docx(bomb.getvalue())
        with self.assertRaises(ValueError): studio.import_file('notes.pdf', b'x')

    def test_pdf_import_extracts_text(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest('pypdf not installed')
        kind, title, content = studio.import_file('research.pdf', build_pdf('Hello Sage'))
        self.assertEqual((kind, title), ('doc', 'research'))
        self.assertIn('Hello Sage', content)

    def test_import_and_export_routes(self):
        data = base64.b64encode(studio.export_xlsx(json.dumps({'cells': {'A1': '5'}}))).decode()
        handled, imported = studio.route_post('/api/studio/import', {'filename': 'data.xlsx', 'data_b64': data})
        self.assertEqual((imported['kind'], imported['title']), ('sheet', 'data'))
        handled, exported = studio.route_post('/api/studio/export', {'kind': 'sheet', 'title': 'My data!', 'content': imported['content'], 'format': 'csv'})
        self.assertEqual((exported['filename'], base64.b64decode(exported['data_b64'])), ('My_data.csv', b'5\n'))
        with self.assertRaises(ValueError): studio.route_post('/api/studio/export', {'kind': 'doc', 'content': 'x', 'format': 'xlsx'})
        with self.assertRaises(ValueError): studio.route_post('/api/studio/import', {'filename': 'a.csv', 'data_b64': '***'})


class CompileTests(unittest.TestCase):
    def test_without_an_engine_the_answer_explains_how_to_install_one(self):
        with patch.object(studio, 'tex_engine', return_value=None):
            result = studio.compile_latex('\\documentclass{article}')
        self.assertFalse(result['ok'])
        self.assertIn('texlive', result['error'])

    def test_log_parsing(self):
        log = './main.tex:12: Undefined control sequence.\nl.12 \\foo\nLaTeX Warning: Reference `x\' on page 1 undefined on input line 30.\nerror: main.tex:5: bad thing\n'
        notes = studio.parse_tex_log(log)
        self.assertIn((12, 'error', 'Undefined control sequence.'), [(n.get('line'), n['severity'], n['message']) for n in notes])
        self.assertTrue(any(n.get('line') == 30 and n['severity'] == 'warn' for n in notes))
        self.assertTrue(any(n.get('line') == 5 for n in notes))

    def test_size_limit(self):
        with self.assertRaises(ValueError): studio.compile_latex('x' * 200_001)

    @unittest.skipIf(os.name == 'nt', 'uses a shell script as a fake LaTeX engine')
    def test_compile_runs_the_engine_in_a_folder_and_returns_the_pdf_and_errors(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            engine = Path(bin_dir) / 'pdflatex'
            engine.write_text('#!/bin/sh\nprintf "%%PDF-1.4 fake" > main.pdf\nprintf "./main.tex:3: Undefined control sequence.\\n" > main.log\nexit 1\n')
            engine.chmod(engine.stat().st_mode | stat.S_IEXEC)
            with patch.object(studio, 'tex_engine', return_value=str(engine)):
                result = studio.compile_latex('\\documentclass{article}\n')
        self.assertFalse(result['ok'])
        self.assertEqual(result['notes'][0]['line'], 3)
        self.assertTrue(base64.b64decode(result['pdf_b64']).startswith(b'%PDF'))


if __name__ == '__main__':
    unittest.main()
