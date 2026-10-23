import unittest
from decimal import Decimal as D, localcontext
from paper_lp_math import (amounts, bounds_from_ticks, hypothetical_allocation,
                           reconcile_frozen, principal_snapshot)


class PaperMathTests(unittest.TestCase):
    def test_exact_closed_form_amounts_and_endpoints(self):
        # sqrt endpoints = 1/2 and 2, L = 2. Independent analytic vector.
        for price, expected in [('0.1',(D(3),D(0))), ('0.25',(D(3),D(0))),
                                ('1',(D(1),D(1))), ('4',(D(0),D(3))), ('10',(D(0),D(3)))]:
            self.assertEqual(amounts(price, '.25', '4', 2), expected)

    def test_fixed_hold_and_divergence(self):
        s = principal_snapshot('1','.25','4','1','1','.25')
        self.assertEqual(s['principal_quote'], D('.75'))
        self.assertEqual(s['hold_quote'], D('1.25'))
        self.assertEqual(s['divergence_quote'], D('-.50'))
        self.assertEqual(s['range_status'], 'OUT_LOW')

    def test_no_peg_or_income_inference(self):
        s = principal_snapshot('1','.25','4','1','1','1')
        for key in ('usd_value','position_fees','rewards','costs','net_return','time_in_range'):
            self.assertIsNone(s[key])
        self.assertFalse(s['usd_peg_assumed'])

    def test_budget_conserved_in_all_states(self):
        for price in ('.1','.25','1','4','10'):
            a=hypothetical_allocation(price,'.25','4','2')
            self.assertAlmostEqual(a['base']*D(price)+a['quote'], D(2), places=25)
            self.assertFalse(a['j0_initialized'])

    def test_tick_orientation_and_decimals(self):
        a,b=bounds_from_ticks(-120,120,18,6,base_is_token0=True,spacing=60)
        x,y=bounds_from_ticks(-120,120,18,6,base_is_token0=False,spacing=60)
        with localcontext() as c:
            c.prec=60
            self.assertLess(abs(a*y-1),D('1e-55'))
            self.assertLess(abs(b*x-1),D('1e-55'))
        self.assertTrue(D('1e11')<a<b<D('1e13'))

    def test_frozen_quantities_not_renormalized(self):
        s=principal_snapshot('1','.25','4','1','1.0000001','2')
        self.assertEqual(s['hold_quote'],D('3.0000001'))
        self.assertGreater(s['implied_liquidity_relative_gap'],0)

    def test_inconsistent_baseline_rejected(self):
        for args in [('1','.25','4','1','2'),('.1','.25','4','1','1'),('1','.25','4','0','0')]:
            with self.assertRaises(ValueError): reconcile_frozen(*args)

    def test_tick_and_decimal_validation(self):
        for ticks in [(-121,120,18,6,60),(-120,120,18,6,0),(-120,120,256,6,60),
                      (120,-120,18,6,60),(-887300,120,18,6,60)]:
            with self.assertRaises(ValueError): bounds_from_ticks(*ticks[:4],base_is_token0=True,spacing=ticks[4])
        with self.assertRaises(ValueError): bounds_from_ticks(-120,120,18,6,base_is_token0='yes',spacing=60)

    def test_invalid_numeric_inputs(self):
        for value in (0,-1,True,1.0,'NaN','Infinity','-Infinity','bad',''):
            with self.assertRaises(ValueError): amounts(value,'.25','4',2)
        with self.assertRaises(ValueError): amounts('1','4','.25',2)

    def test_precision_independent_of_caller(self):
        with localcontext() as c:
            c.prec=6
            low=principal_snapshot('1','.25','4','1','1','2')['principal_quote']
        with localcontext() as c:
            c.prec=60
            high=principal_snapshot('1','.25','4','1','1','2')['principal_quote']
        self.assertEqual(low,high)


if __name__ == '__main__': unittest.main()
