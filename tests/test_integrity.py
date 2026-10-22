import unittest
from datetime import datetime
from discover_uniswap_positions import inventory, MANAGER, PublicRpc
from lp_state import classify_position
from audit_paper_registry import audit

OWNER = "0x"+"11"*20
OTHER = "0x"+"22"*20

def abi(*values):
    return "0x"+"".join(f"{v:064x}" for v in values)

class FakeRpc:
    def __init__(self, entries=(), fail_index=None, wrong_owner=False, chain=8453):
        self.entries=entries;self.calls=[];self.fail_index=fail_index
        self.wrong_owner=wrong_owner;self.chain=chain
    def request(self, method, params):
        self.calls.append((method,params))
        if method=="eth_chainId":return hex(self.chain)
        if method=="eth_blockNumber":return "0x12345"
        assert method=="eth_call" and params[1]=="0x12345"
        assert params[0]["to"]==MANAGER
        data=params[0]["data"];sel=data[2:10]
        if sel=="70a08231":return abi(len(self.entries))
        if sel=="2f745c59":
            i=int(data[-64:],16)
            if i==self.fail_index:raise RuntimeError("RPC unavailable")
            return abi(self.entries[i][0])
        nft=int(data[-64:],16)
        if sel=="6352211e":return abi(int(OTHER if self.wrong_owner else OWNER,16))
        if sel=="99fbab88":
            _,liquidity,owed=next(e for e in self.entries if e[0]==nft)
            return abi(0,0,1,2,3000,(-198900)%(2**256),(-196920)%(2**256),liquidity,0,0,owed,0)
        raise AssertionError(sel)

class InventoryTests(unittest.TestCase):
    def test_empty_scoped_wallet_is_complete_but_never_capital_permission(self):
        x=inventory(FakeRpc(),OWNER)
        self.assertTrue(x["coverage_complete"]);self.assertEqual(x["active_nft_ids"],[])
        self.assertFalse(x["automatic_capital_update_authorized"])
    def test_active_empty_and_tokens_owed_are_distinct(self):
        rpc=FakeRpc([(1,50,0),(2,0,0),(3,0,25)])
        x=inventory(rpc,OWNER)
        self.assertEqual(x["active_nft_ids"],[1])
        self.assertEqual([p["activity_status"] for p in x["positions"]],
            ["ACTIVE_LIQUIDITY","EMPTY_POSITION","NO_LIQUIDITY_TOKENS_OWED"])
        self.assertEqual(x["positions"][0]["tick_lower"],-198900)
    def test_failed_index_never_claims_exhaustive_absence(self):
        x=inventory(FakeRpc([(1,0,0),(2,30,0)],fail_index=1),OWNER)
        self.assertEqual(x["status"],"PARTIAL");self.assertIsNone(x["active_nft_ids"])
    def test_limit_is_reported_not_silent_truncation(self):
        x=inventory(FakeRpc([(1,0,0),(2,30,0)]),OWNER,max_positions=1)
        self.assertFalse(x["coverage_complete"]);self.assertIsNone(x["active_nft_ids"])
    def test_wrong_owner_is_never_accepted(self):
        x=inventory(FakeRpc([(1,20,0)],wrong_owner=True),OWNER)
        self.assertFalse(x["coverage_complete"]);self.assertEqual(x["positions"],[])
    def test_wrong_chain_fails_before_any_position_read(self):
        rpc=FakeRpc(chain=1);x=inventory(rpc,OWNER)
        self.assertEqual(x["status"],"ERROR");self.assertEqual(len(rpc.calls),1)
    def test_duplicate_enumeration_fails_closed(self):
        x=inventory(FakeRpc([(1,20,0),(1,20,0)]),OWNER)
        self.assertFalse(x["coverage_complete"])
    def test_transport_forbids_transaction_methods_before_network(self):
        with self.assertRaises(ValueError):PublicRpc("https://example.invalid").request("eth_sendRawTransaction",[])
    def test_nonnegative_liquidity_required(self):
        for value in (-1,True,"0"):
            with self.assertRaises(ValueError):classify_position(value)

class PaperTests(unittest.TestCase):
    now=datetime.fromisoformat("2026-10-22T15:50:00-03:00")
    def test_all_historical_formats_count_once(self):
        lines=["RUN_ID: RADAR-20261003-1103-Bahia",
            "RADAR-20261003-1800-Bahia | run=observed",
            "RUN | RADAR-20261008-0505-Bahia | run=observed",
            "SNAPSHOT | RADAR-20261022-1210-Bahia | test",
            "SNAPSHOT_PAPER | RADAR-20261022-1504-Bahia | test"]
        result=audit("\n".join(lines),self.now)
        self.assertEqual(result["snapshot_records"],5)
        self.assertEqual(sum(not m["mature"] for m in result["missing_pairs"]),3)
    def test_duplicate_pair_detected_across_old_and_new_eval_formats(self):
        rid="RADAR-20261003-1103-Bahia"
        result=audit(f"RUN_ID: {rid}\nEVAL | {rid} | H+1 | NO_TRIGGER\nEVAL_PAPER | {rid} | H+1 | NO_TRIGGER",self.now)
        self.assertEqual(result["duplicate_pairs"],[[rid,"H+1"]])
    def test_malformed_snapshot_not_repaired(self):
        x=audit("SNAPSHOT_PAPER | RADAR-202610122009-Bahia | test",self.now)
        self.assertEqual(x["snapshot_records"],0);self.assertEqual(len(x["malformed_records"]),1)
    def test_glued_eval_is_counted_once_and_flagged(self):
        rid="RADAR-20261003-1103-Bahia"
        x=audit(f"SNAPSHOT_PAPER | {rid} | BTC={{class=NONE}}EVAL_PAPER | {rid} | H+4 | NO_TRIGGER",self.now)
        self.assertEqual(x["evaluation_records"],1)
        self.assertEqual(len(x["glued_evaluations"]),1)
        self.assertEqual([m["horizon"] for m in x["missing_pairs"]],["H+1"])
    def test_orphan_evaluation_and_naive_time_rejected(self):
        x=audit("EVAL | RADAR-20261003-1103-Bahia | H+1 | NO_TRIGGER",self.now)
        self.assertEqual(len(x["orphan_evaluations"]),1)
        with self.assertRaises(ValueError):audit("",datetime(2026,10,22))

if __name__=="__main__":unittest.main()
