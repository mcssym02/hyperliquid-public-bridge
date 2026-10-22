import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from collect_paper_pools import POOLS, RPC_URLS, observe_chain, summarize
from refresh_paper_pools import refresh, validate_snapshot, TIMEOUT_SECONDS
from test_paper_pools import FakeRpc, NOW

def fixture():
    chains = [observe_chain(FakeRpc([p for p in POOLS if p["chain"] == c]),c,[p for p in POOLS if p["chain"] == c],NOW) for c in RPC_URLS]
    x=summarize(chains)
    x["timestamp_utc"]=datetime.fromtimestamp(NOW, timezone.utc).isoformat()
    return x

class RefreshTests(unittest.TestCase):
    def publish(self, payload=None, *, code=0, error=None, malformed=False):
        def runner(command, **kwargs):
            self.assertEqual(kwargs["timeout"],20)
            self.assertEqual(kwargs["stdout"],subprocess.DEVNULL)
            self.assertNotIn("shell",kwargs)
            self.assertNotIn("--owner",command)
            if error:raise error
            Path(command[-1]).write_text("broken" if malformed else json.dumps(payload))
            return SimpleNamespace(returncode=code)
        with tempfile.TemporaryDirectory() as work:
            target=Path(work)/"data.json"
            target.write_text(json.dumps({"status":"OK","quote_per_base":"STALE_SECRET_SENTINEL"}))
            x=refresh(target,runner=runner,now=NOW)
            self.assertEqual(json.loads(target.read_text()),x)
            self.assertNotIn("STALE_SECRET_SENTINEL",target.read_text())
            self.assertFalse(x["refresh"]["previous_prices_reused"])
            return x
    def test_five_verified_fresh_pools_publish_exact_quote_values(self):
        p=fixture();x=self.publish(p)
        self.assertEqual(x["chains"],p["chains"])
        self.assertTrue(x["coverage_complete"])
    def test_partial_source_preserves_only_verified_pool_prices(self):
        p=fixture();p["chains"][1]["pools"][0].update(status="ERROR",identity_verified=False,quote_per_base=None)
        p["chains"][1]["status"]="PARTIAL"
        p.update(status="PARTIAL",coverage_complete=False,verified_pool_count=4,synchronized_within_60s=False)
        x=self.publish(p,code=2)
        self.assertEqual(x["verified_pool_count"],4)
        self.assertIsNone(x["chains"][1]["pools"][0]["quote_per_base"])
    def test_timeout_emits_nulls_and_explicit_error_instead_of_old_prices(self):
        x=self.publish(error=subprocess.TimeoutExpired("collector",TIMEOUT_SECONDS))
        self.assertEqual(x["status"],"ERROR");self.assertEqual(x["chains"],[])
        self.assertEqual(x["errors"],["COLLECTOR_TIMEOUT_20S"])
        self.assertFalse(x["automatic_capital_update_authorized"])
    def test_crash_and_malformed_json_degrade_without_raising(self):
        for kwargs in (dict(code=1),dict(malformed=True),dict(error=OSError("process unavailable"))):
            with self.subTest(kwargs=kwargs):
                x=self.publish(fixture(),**kwargs)
                self.assertEqual(x["status"],"ERROR");self.assertEqual(x["verified_pool_count"],0)
    def test_stale_snapshot_or_source_block_rejected(self):
        for location in ("snapshot","block"):
            p=fixture()
            old=datetime.fromtimestamp(NOW-181,timezone.utc).isoformat()
            if location=="snapshot":p["timestamp_utc"]=old
            else:p["chains"][0]["block_timestamp_utc"]=old
            self.assertEqual(self.publish(p)["status"],"ERROR")
    def test_quote_identity_fee_economic_and_coverage_corruption_rejected(self):
        mutations=[lambda p:p["chains"][0]["pools"][0].update(price_unit="USD/WETH"),
            lambda p:p["chains"][0]["pools"][0].update(token0="0x"+"11"*20),
            lambda p:p["chains"][0]["pools"][0].update(fee_raw=500),
            lambda p:p["chains"][0]["pools"][0].update(position_fees="100"),
            lambda p:p.update(verified_pool_count=6),
            lambda p:p.update(usd_peg_assumed=True),
            lambda p:p.update(paper_j0_initialized=True),
            lambda p:p.update(automatic_capital_update_authorized=True)]
        for change in mutations:
            p=fixture();change(p)
            self.assertEqual(self.publish(p)["status"],"ERROR")
    def test_no_unverified_row_can_keep_a_price(self):
        p=fixture();p["chains"][0]["pools"][0]["status"]="ERROR"
        with self.assertRaises(ValueError):validate_snapshot(p,NOW)
    def test_nan_infinity_and_nonpositive_prices_rejected(self):
        for price in ("NaN","Infinity","0","-1"):
            p=fixture();p["chains"][0]["pools"][0]["quote_per_base"]=price
            with self.assertRaises(ValueError):validate_snapshot(p,NOW)
    def test_duplicate_pool_and_future_or_naive_clock_rejected(self):
        p=fixture();p["chains"][0]["pools"].append(copy.deepcopy(p["chains"][0]["pools"][0]))
        with self.assertRaises(ValueError):validate_snapshot(p,NOW)
        for time in ("2026-10-22T00:00:00",datetime.fromtimestamp(NOW+31,timezone.utc).isoformat()):
            p=fixture();p["timestamp_utc"]=time
            with self.assertRaises(ValueError):validate_snapshot(p,NOW)

if __name__ == "__main__":unittest.main()
