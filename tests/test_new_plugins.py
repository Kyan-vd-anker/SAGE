import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage import config, db, plugins

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False
try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

def build_pdf(text):
    """Build a tiny valid one-page PDF with correct xref offsets."""
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


class NewPluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=self.root, DB_PATH=self.root / 'sage.db', VAULT=self.root / 'vault', WORKSPACE=self.root / 'workspace',
                                 PLUGIN_DIR=Path(__file__).resolve().parents[1] / 'plugins')
        patcher.start(); self.addCleanup(patcher.stop); db.init()
        (self.root / 'workspace').mkdir(parents=True, exist_ok=True)

    def fallback(self):
        return patch.dict(sys.modules, {'CoolProp': None, 'CoolProp.CoolProp': None})

    def test_fluid_properties_fallback_water_at_20c(self):
        with self.fallback():
            result = plugins.call('fluid_properties', {'fluid': 'Water', 'temperature_c': 20})['result']
        self.assertAlmostEqual(result['density_kg_m3'], 998.2, delta=0.5)
        self.assertAlmostEqual(result['prandtl'], 7.0, delta=0.2)
        self.assertIn('Approximate', result['note'])

    def test_fluid_properties_fallback_interpolates_ethanol_and_air(self):
        with self.fallback():
            ethanol = plugins.call('fluid_properties', {'fluid': 'ethanol', 'temperature_c': 50})['result']
            air = plugins.call('fluid_properties', {'fluid': 'air', 'temperature_c': 27})['result']
        self.assertTrue(0.59 < ethanol['dynamic_viscosity_mpa_s'] < 0.83)
        self.assertAlmostEqual(air['density_kg_m3'], 1.18, delta=0.03)
        self.assertAlmostEqual(air['dynamic_viscosity_pa_s'], 1.85e-5, delta=0.1e-5)

    def test_fluid_properties_rejects_bad_input(self):
        with self.fallback():
            with self.assertRaises(ValueError): plugins.call('fluid_properties', {'fluid': 'Water', 'temperature_c': 150})
            with self.assertRaises(ValueError): plugins.call('fluid_properties', {'fluid': 'Mercury', 'temperature_c': 20})
            with self.assertRaises(ValueError): plugins.call('fluid_properties', {'fluid': '../x', 'temperature_c': 20})

    def test_plot_maker_needs_approval_then_writes_png(self):
        args = {'title': 'Test plot', 'x': [1, 2, 3], 'series': {'a': [1, 4, 9]}, 'xlabel': 'x', 'ylabel': 'y'}
        self.assertEqual(plugins.call('plot_maker', dict(args))['status'], 'pending_approval')
        if not HAVE_MPL: self.skipTest('matplotlib not installed')
        result = plugins.call('plot_maker', dict(args), approved=True)['result']
        png = self.root / 'workspace' / result['path']
        self.assertTrue(png.is_file() and png.read_bytes().startswith(b'\x89PNG'))
        again = plugins.call('plot_maker', dict(args), approved=True)['result']
        self.assertNotEqual(result['path'], again['path'])

    @unittest.skipUnless(HAVE_MPL, 'matplotlib not installed')
    def test_plot_maker_csv_bar_and_validation(self):
        (self.root / 'workspace' / 'd.csv').write_text('T,Q1,Q2\n10,1,2\n20,2,3\n30,3,5\n', encoding='utf-8')
        result = plugins.call('plot_maker', {'csv_path': 'd.csv', 'kind': 'bar', 'filename': 'csv bar'}, approved=True)['result']
        self.assertEqual(result['series'], ['Q1', 'Q2'])
        self.assertTrue(result['path'].startswith('plots/csv_bar'))
        with self.assertRaises(ValueError): plugins.call('plot_maker', {'x': [1, 2], 'series': {'a': [1]}}, approved=True)
        with self.assertRaises(ValueError): plugins.call('plot_maker', {'x': [1, 'oops'], 'series': {'a': [1, 2]}}, approved=True)
        with self.assertRaises(ValueError): plugins.call('plot_maker', {'csv_path': '../d.csv'}, approved=True)
        with self.assertRaises(ValueError): plugins.call('plot_maker', {'title': 'nothing'}, approved=True)

    @unittest.skipUnless(HAVE_PYPDF, 'pypdf not installed')
    def test_pdf_reader_reads_text_and_stays_in_workspace(self):
        (self.root / 'workspace' / 'a.pdf').write_bytes(build_pdf('Hello Sage'))
        result = plugins.call('pdf_reader', {'path': 'a.pdf'})['result']
        self.assertEqual(result['pages_total'], 1)
        self.assertIn('Hello Sage', result['text'])
        with self.assertRaises(ValueError): plugins.call('pdf_reader', {'path': '../a.pdf'})
        (self.root / 'workspace' / 'b.txt').write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError): plugins.call('pdf_reader', {'path': 'b.txt'})
        with self.assertRaises(ValueError): plugins.call('pdf_reader', {'path': 'a.pdf', 'first_page': 5})


if __name__ == '__main__':
    unittest.main()
