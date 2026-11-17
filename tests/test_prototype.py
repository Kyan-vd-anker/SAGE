import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sage import config, db, prototype, routes


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db', VAULT=root / 'vault',
                                 WORKSPACE=root / 'workspace')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()
        self.plan = {
            'summary': 'Read a temperature sensor and display its value.',
            'target': 'ESP32',
            'firmware_language': 'Arduino C++',
            'requirements': [{'id': 'R1', 'text': 'Read sensor temperature', 'verification': 'Compare with a reference thermometer.'}],
            'assumptions': ['Sensor voltage compatibility needs confirmation.'],
            'components': [{'ref': 'U1', 'name': 'ESP32 development board', 'part_number': '', 'quantity': 1,
                            'role': 'Microcontroller', 'status': 'candidate'}],
            'pin_map': [{'board_pin': 'GPIO21', 'signal': 'I2C SDA', 'connects_to': 'Sensor SDA', 'notes': ''}],
            'connections': [],
            'validation_steps': ['Check voltage levels before powering on.'],
        }
        self.schematic_spec = {
            'components': [{'id': 'U1', 'symbol': 'ic', 'label': 'ESP32', 'x': 0, 'y': 0, 'angle': 0,
                            'pins': [{'number': '1', 'name': 'SDA', 'side': 'L', 'anchor': 'sda'},
                                     {'number': '2', 'name': 'GND', 'side': 'B', 'anchor': 'gnd'}]}],
            'wires': [], 'junctions': [],
        }
        self.source = 'cube([20, 20, 2], center=true);\n'

    def package(self, summary):
        plan = dict(self.plan, summary=summary)
        return {'plan': plan, 'firmware': 'void setup() {}\nvoid loop() {}\n',
                'schematic_spec': self.schematic_spec, 'openscad_source': self.source}

    def test_plan_rejects_duplicate_references_and_non_integer_quantities(self):
        invalid = dict(self.plan)
        invalid['components'] = [dict(self.plan['components'][0]), dict(self.plan['components'][0])]
        with self.assertRaisesRegex(ValueError, 'references must be unique'):
            prototype._validate_plan(invalid)
        invalid = dict(self.plan)
        invalid['components'] = [dict(self.plan['components'][0], quantity=True)]
        with self.assertRaisesRegex(ValueError, 'quantity must be an integer'):
            prototype._validate_plan(invalid)

    def test_create_and_revise_save_immutable_package_versions(self):
        with patch('sage.prototype._generate_package', side_effect=[self.package('Original plan'), self.package('Updated plan')]), \
                patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'), \
                patch('sage.openscad._render', return_value=('', 'OpenSCAD missing')):
            created = prototype.create_design('Sensor prototype', prompt='Create a sensor prototype')
            revised = prototype.revise_design(created['design']['id'], 'Add a status LED')

        self.assertEqual((created['version']['version'], revised['version']['version']), (1, 2))
        self.assertEqual(created['version']['plan']['summary'], 'Original plan')
        self.assertEqual(revised['version']['plan']['summary'], 'Updated plan')
        self.assertEqual([item['version'] for item in prototype._design(created['design']['id'])['versions']], [2, 1])
        self.assertTrue(revised['version']['render_error'])

    def test_version_route_returns_the_validated_artifact_package(self):
        with patch('sage.prototype._generate_package', return_value=self.package('Route test')), \
                patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'), \
                patch('sage.openscad._render', return_value=('', '')):
            handled, result = routes.post('/api/prototypes', {
                'title': 'Sensor prototype', 'prompt': 'Build a temperature sensor prototype',
            })
        self.assertTrue(handled)
        retrieved, version = routes.get(f"/api/prototypes/versions/{result['version']['id']}", {})
        self.assertTrue(retrieved)
        self.assertEqual(version['plan']['target'], 'ESP32')
        self.assertIn('cube([20, 20, 2], center=true)', version['openscad_source'])

    def test_edited_artifacts_are_saved_as_a_new_version(self):
        with patch('sage.prototype._generate_package', return_value=self.package('Editable plan')), \
                patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'), \
                patch('sage.openscad._render', return_value=('', '')):
            created = prototype.create_design('Editable prototype', prompt='Create a sensor prototype')
            edited = prototype.save_version(created['design']['id'], {
                'plan': dict(self.plan, summary='User-edited plan'),
                'firmware': 'void setup() { pinMode(2, OUTPUT); }\nvoid loop() {}\n',
                'schematic_spec': self.schematic_spec,
                'openscad_source': 'cube([24, 20, 2], center=true);',
                'notes': 'Adjusted the enclosure width',
                'prompt': 'Create a sensor prototype',
            })

        self.assertEqual(edited['version']['version'], 2)
        self.assertEqual(edited['version']['notes'], 'Adjusted the enclosure width')
        self.assertEqual(edited['version']['plan']['summary'], 'User-edited plan')
        self.assertIn('cube([24, 20, 2], center=true)', edited['version']['openscad_source'])

    def test_mesh_path_rejects_a_database_name_outside_mesh_directory(self):
        with patch('sage.prototype._generate_package', return_value=self.package('Unsafe mesh')), \
                patch('sage.schematic._render_svg', return_value='<svg xmlns="http://www.w3.org/2000/svg"></svg>'), \
                patch('sage.openscad._render', return_value=('../outside.stl', '')):
            created = prototype.create_design('Unsafe mesh', prompt='Make a small enclosure')
        with self.assertRaises(ValueError):
            prototype.mesh_path(created['version']['id'])


if __name__ == '__main__':
    unittest.main()
