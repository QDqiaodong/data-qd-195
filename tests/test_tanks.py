import json
from pathlib import Path
import unittest
from tank_transfer.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class TankTests(unittest.TestCase):
    def test_simultaneous_opening_concentration_then_next_step(self):
        r=process(sample())
        self.assertEqual(r['steps'][0]['closing']['C']['mass_g']['salt'],'0')
        self.assertEqual(r['final']['B']['mass_g']['salt'],'35')
        self.assertEqual(r['final']['C']['mass_g']['salt'],'15')
        self.assertEqual(r['final']['C']['concentration_g_per_l']['salt'],'15/8')
        self.assertEqual(r['summary'],dict(tanks=3,species=1,steps=2,transfers=3,total_volume_l='20',total_mass_g={'salt':'100'},conserved=True))

    def test_transfer_order_does_not_change_physics(self):
        d=sample();d['steps'][0]['transfers'].reverse()
        self.assertEqual(process(d)['final'],process(sample())['final'])

    def test_incoming_cannot_fund_same_step_outflow(self):
        d=sample();d['tanks'][1].update(volume_l='0');d['tanks'][0]['volume_l']='10'
        with self.assertRaisesRegex(ValueError,'opening volume insufficient'):process(d)

    def test_closing_capacity_not_transient_order(self):
        d=sample();d['tanks'][1]['capacity_l']='10'
        self.assertTrue(process(d)['summary']['conserved'])
        d['tanks'][2]['capacity_l']='7'
        with self.assertRaisesRegex(ValueError,'closing capacity exceeded'):process(d)

    def test_multiple_species_exact_fraction_and_empty_tank(self):
        d=sample();d['species'].append('dye')
        for tank in d['tanks']:tank['mass_g']['dye']='1' if tank['id']=='A' else '0'
        d['steps']=[dict(id='drain',transfers=[dict(id='all',source='A',target='C',volume_l='10')])]
        r=process(d)
        self.assertIsNone(r['final']['A']['concentration_g_per_l']['dye'])
        self.assertEqual(r['final']['C']['concentration_g_per_l']['dye'],'1/10')
        self.assertTrue(all(v=='0' for v in r['steps'][0]['global_mass_residual_g'].values()))

    def test_initial_mass_zero_volume_duplicate_and_unknown(self):
        d=sample();d['tanks'][2]['mass_g']['salt']='1'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['steps'][0]['transfers'][1]['id']='ab'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['steps'][0]['transfers'][1]['target']='missing'
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
