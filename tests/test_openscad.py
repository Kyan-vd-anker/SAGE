import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sage import agent, config, db, openscad, routes, server


class OpenScadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault',
                                 WORKSPACE=root / 'workspace', OPENSCAD_PATH='openscad')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()
        self.source = 'width = 20;\ncube([width, 10, 4], center=true);\n'

    def test_created_and_edited_models_are_immutable_versions(self):
        with patch('sage.openscad._render', return_value=('preview.stl', '')):
            created = openscad.create_design('Mount', prompt='Make a small mount', source=self.source)
            edited = openscad.save_source(created['design']['id'], self.source.replace('width = 20', 'width = 24'), 'Wider')

        self.assertEqual((created['version']['version'], edited['version']['version']), (1, 2))
        self.assertNotEqual(created['version']['source'], edited['version']['source'])
        self.assertEqual([item['version'] for item in openscad._design(created['design']['id'])['versions']], [2, 1])

    def test_ai_revision_uses_saved_source_and_creates_a_new_version(self):
        with patch('sage.openscad._render', return_value=('', 'OpenSCAD missing')) as render, \
                patch('sage.openscad.llm.chat_message', return_value={'content': '```scad\ncube([24, 10, 4]);\n```'}) as model:
            created = openscad.create_design('Mount', prompt='Make a mount', source=self.source)
            result = openscad.revise_design(created['design']['id'], 'Make it wider')

        self.assertEqual(result['version']['version'], 2)
        self.assertIn(self.source, model.call_args.args[0][1]['content'])
        self.assertIn('cube([24, 10, 4])', result['version']['source'])
        self.assertEqual(model.call_args.kwargs['model'], config.CODING_MODEL)
        self.assertEqual(render.call_count, 2)

    def test_schematic_is_analyzed_before_generating_the_model(self):
        image = {'name': 'diagram.png', 'data_b64': base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode('ascii')}
        with patch('sage.openscad._render', return_value=('', '')) , \
                patch('sage.openscad.llm.describe_image', return_value='A rectangular housing with two mounting holes.') as vision, \
                patch('sage.openscad.llm.chat_message', return_value={'content': self.source}) as model:
            openscad.create_design('Housing', prompt='Model this housing', images=[image])

        vision.assert_called_once()
        self.assertIn('two mounting holes', model.call_args.args[0][1]['content'])

    def test_missing_openscad_keeps_source_and_reports_preview_error(self):
        with patch('sage.openscad._executable', return_value=None):
            result = openscad.create_design('Mount', prompt='Make a mount', source=self.source)

        self.assertIn('OpenSCAD was not found', result['version']['render_error'])
        self.assertEqual(openscad._version(result['version']['id'])['source'], self.source)
        self.assertIsNone(openscad.mesh_path(result['version']['id']))

    def test_dimensioned_orthographic_drawing_is_saved_per_model_version(self):
        mesh_name = 'mount.stl'
        with patch('sage.openscad._render', return_value=(mesh_name, '')):
            created = openscad.create_design('Mount', prompt='Make a mount', source=self.source)
        mesh = config.DATA / 'engineering' / 'openscad' / 'meshes' / mesh_name
        mesh.parent.mkdir(parents=True, exist_ok=True)
        mesh.write_text(
            'solid mount\n'
            'facet normal 0 0 1 outer loop\n'
            'vertex 0 0 0\nvertex 10 0 0\nvertex 0 20 30\n'
            'endloop endfacet\nendsolid mount\n',
            encoding='ascii',
        )
        scripts = []

        def fake_openscad(command, **_kwargs):
            scripts.append(Path(command[-1]).read_text(encoding='utf-8'))
            Path(command[2]).write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding='utf-8')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch('sage.openscad._executable', return_value='openscad'), \
                patch('sage.openscad.subprocess.run', side_effect=fake_openscad) as render:
            drawing = openscad.create_technical_drawing(created['version']['id'])
            cached = openscad.create_technical_drawing(created['version']['id'])

        self.assertEqual(drawing['dimensions_mm'], {'x': 10.0, 'y': 20.0, 'z': 30.0})
        self.assertIn('<svg', drawing['svg'])
        self.assertEqual(cached['svg'], drawing['svg'])
        self.assertEqual(render.call_count, 1)
        self.assertEqual(scripts[0].count('projection(cut=false)'), 3)
        self.assertIn('X 10 mm', scripts[0])
        self.assertIn('Y 20 mm', scripts[0])
        self.assertIn('Z 30 mm', scripts[0])

    def test_external_file_references_are_rejected(self):
        for source in ('include <private.scad>\ncube(1);', 'use "private.scad";\ncube(1);',
                       'import("outside.stl");', 'surface(file="private.dat");'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'External file'):
                openscad.create_design('Unsafe', source=source)
        self.assertEqual(openscad.list_designs(), [])

    def test_api_routes_create_and_retrieve_a_design(self):
        with patch('sage.openscad._render', return_value=('', '')):
            handled, result = routes.post('/api/openscad/designs', {'title': 'Mount', 'source': self.source})

        self.assertTrue(handled)
        retrieved, design = routes.get(f"/api/openscad/designs/{result['design']['id']}", {})
        self.assertTrue(retrieved)
        self.assertEqual(design['versions'][0]['version'], 1)

    def test_chat_image_upload_is_described_as_temporary_context(self):
        image = {'name': 'diagram.png', 'data_b64': base64.b64encode(b'\x89PNG\r\n\x1a\nimage').decode('ascii')}
        with patch('sage.server.llm.describe_image', return_value='A labeled wiring schematic.') as vision:
            context = server.Handler._selected_image_context(None, [image], 'What is this?')

        self.assertIn('labeled wiring schematic', context)
        vision.assert_called_once()

    def test_chat_image_context_is_passed_as_reference_not_a_new_request(self):
        with patch('sage.agent.memory.capture', return_value=[]), \
                patch('sage.agent._relevant_context', return_value=([], [])), \
                patch('sage.agent.tools.definitions_for_query', return_value=[]), \
                patch('sage.agent.llm.chat_message', return_value={'content': 'A connector is visible.'}) as model:
            answer = agent.respond('What is shown?', image_context='A schematic with a 10k resistor.')

        self.assertEqual(answer, 'A connector is visible.')
        system = model.call_args.args[0][0]['content']
        self.assertIn('temporary visual analysis', system)
        self.assertIn('10k resistor', system)


if __name__ == '__main__':
    unittest.main()
