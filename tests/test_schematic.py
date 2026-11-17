import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage import config, db, routes, schematic

try:
    import schemdraw
    HAS_SCHEMDRAW = True
except ImportError:
    HAS_SCHEMDRAW = False


class SchematicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault',
                                 WORKSPACE=root / 'workspace')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()
        self.spec = {
            'components': [
                {'id': 'R1', 'symbol': 'resistor', 'label': '1 kOhm', 'x': 0, 'y': 0, 'angle': 0},
                {'id': 'V1', 'symbol': 'voltage_source', 'label': '5 V', 'x': 5, 'y': 0, 'angle': 0},
            ],
            'wires': [{'from': {'component': 'R1', 'anchor': 'end'},
                       'to': {'component': 'V1', 'anchor': 'start'}, 'via': []}],
            'junctions': [],
        }

    def test_schematic_is_rendered_and_versions_are_persisted(self):
        if not HAS_SCHEMDRAW:
            self.skipTest('schemdraw is not installed')
        created = schematic.create_design('LED circuit', prompt='A 5 V circuit with a resistor', spec=self.spec)
        self.assertIn('<svg', created['version']['svg'])
        self.assertEqual(created['version']['spec']['components'][0]['symbol'], 'resistor')
        self.assertEqual(created['design']['versions'][0]['version'], 1)

    def test_ai_revision_builds_an_immutable_version(self):
        reply_spec = dict(self.spec)
        reply_spec['components'] = [dict(component) for component in self.spec['components']]
        reply_spec['components'][0]['label'] = '2 kOhm'
        with patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'), \
                patch('sage.schematic.llm.chat_message', return_value={'content': json.dumps(reply_spec)}) as model:
            created = schematic.create_design('LED circuit', prompt='Make a circuit', spec=self.spec)
            revised = schematic.revise_design(created['design']['id'], 'Change the resistor to 2 kOhm')

        self.assertEqual(revised['version']['version'], 2)
        self.assertEqual(revised['version']['spec']['components'][0]['label'], '2 kOhm')
        self.assertIn('"components"', model.call_args.args[0][1]['content'])

    def test_component_symbols_anchors_and_coordinates_are_validated(self):
        invalid = dict(self.spec)
        invalid['components'] = [dict(self.spec['components'][0], symbol='python')]
        with self.assertRaisesRegex(ValueError, 'Unsupported schematic symbol'):
            schematic._validate_spec(invalid)
        invalid = dict(self.spec)
        invalid['wires'] = [{'from': {'component': 'R1', 'anchor': 'base'},
                             'to': {'component': 'V1', 'anchor': 'start'}}]
        with self.assertRaisesRegex(ValueError, 'valid component anchor'):
            schematic._validate_spec(invalid)
        invalid = dict(self.spec)
        invalid['components'] = [dict(self.spec['components'][0], x=1000)]
        with self.assertRaisesRegex(ValueError, 'coordinates must be finite'):
            schematic._validate_spec(invalid)

    def test_generic_ic_pin_anchors_connect_to_motor_and_render(self):
        spec = {
            'components': [
                {'id': 'U1', 'symbol': 'ic', 'label': 'Controller', 'x': 0, 'y': 0, 'angle': 0,
                 'pins': [{'number': '1', 'name': 'MOTOR_EN', 'side': 'R', 'anchor': 'motor_en'},
                          {'number': '2', 'name': 'GND', 'side': 'B', 'anchor': 'gnd'}]},
                {'id': 'M1', 'symbol': 'motor', 'label': 'DC motor', 'x': 8, 'y': 0, 'angle': 0},
            ],
            'wires': [{'from': {'component': 'U1', 'anchor': 'motor_en'},
                       'to': {'component': 'M1', 'anchor': 'start'}, 'via': []}],
            'junctions': [],
        }
        clean = schematic._validate_spec(spec)
        self.assertEqual(clean['components'][0]['pins'][0]['anchor'], 'motor_en')
        self.assertEqual(clean['wires'][0]['to']['anchor'], 'start')
        if not HAS_SCHEMDRAW:
            self.skipTest('schemdraw is not installed')
        svg = schematic._render_svg(clean)
        self.assertIn('<svg', svg)

    def test_http_routes_create_and_load_schematic_versions(self):
        with patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'):
            handled, result = routes.post('/api/schematics/designs', {
                'title': 'LED circuit', 'prompt': 'Draw an LED circuit', 'spec': self.spec,
            })
        self.assertTrue(handled)
        retrieved, version = routes.get(f"/api/schematics/versions/{result['version']['id']}", {})
        self.assertTrue(retrieved)
        self.assertEqual(version['spec']['components'][1]['id'], 'V1')


if __name__ == '__main__':
    unittest.main()
