import copy
from decimal import Decimal, localcontext
import unittest
from collect_paper_pools import (POOLS, SELECTORS, RPC_URLS, PublicReadRpc,
    observe_pool, observe_chain, summarize, abi_words, int24)

NOW = 1792699200
BLOCK = dict(number="0x12345", timestamp=hex(NOW-5), hash="0x"+"11"*32)

def abi(*values):
    return "0x"+"".join(f"{v % 2**256:064x}" for v in values)

class FakeRpc:
    def __init__(self, pools, chain=None, fail_pool=None, reorg=False, stale=False,
                 bad_decimals=False, wrong_tokens=False, wrong_fee=False, zero_price=False):
        self.pools=pools; self.chain=chain or pools[0]["chain"]; self.calls=[]
        self.fail_pool=fail_pool; self.reorg=reorg; self.stale=stale
        self.bad_decimals=bad_decimals; self.wrong_tokens=wrong_tokens
        self.wrong_fee=wrong_fee; self.zero_price=zero_price
    def request(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":return hex(self.chain)
        if method == "eth_getBlockByNumber":
            b=copy.copy(BLOCK)
            if self.stale:b["timestamp"]=hex(NOW-181)
            if self.reorg and params[0] != "latest":b["hash"]="0x"+"22"*32
            return b
        assert method == "eth_call" and params[1] == BLOCK["number"]
        target=params[0]["to"]; selector=params[0]["data"]
        if selector == SELECTORS["decimals"]:
            d=next(p["decimals"][target] for p in self.pools if target in p["decimals"])
            return abi(d+1 if self.bad_decimals else d)
        pool=next(p for p in self.pools if p["address"] == target)
        if pool["id"] == self.fail_pool:raise RuntimeError("pool unavailable")
        tokens=sorted(pool["decimals"])
        if selector in [SELECTORS["token0"], SELECTORS["token1"]]:
            index=0 if selector == SELECTORS["token0"] else 1
            return abi(1 if self.wrong_tokens else int(tokens[index], 16))
        if selector == SELECTORS["fee"]:return abi(2500 if self.wrong_fee else pool["fee"] if pool["fee"] is not None else 437)
        if selector == SELECTORS["tickSpacing"]:return abi(pool["spacing"] or 100)
        if selector == SELECTORS["liquidity"]:return abi(1000000)
        if selector == SELECTORS["slot0"]:
            # Decimal ratio of one whole token1 per token0. Nonzero/negative tick.
            tick=-198000
            with localcontext() as ctx:
                ctx.prec=70
                sqrt=int(Decimal("1.0001")**(Decimal(tick)/2)*2**96)+1
            return abi(0 if self.zero_price else sqrt, tick, 0, 1, 1, *([0] if pool["dex"] == "uniswap-v3" else []), 1)
        raise AssertionError(selector)

class PoolTests(unittest.TestCase):
    def setUp(self):self.base=[p for p in POOLS if p["chain"] == 8453]
    def test_exact_five_manifest_has_unique_pools_and_no_wallet_or_j0(self):
        self.assertEqual(len({p["address"] for p in POOLS}),5)
        self.assertEqual({p["chain"] for p in POOLS},{8453,4663})
        self.assertNotIn("owner",repr(POOLS));self.assertNotIn("J0",repr(POOLS))
    def test_pinned_identity_price_direction_and_dynamic_fee(self):
        rpc=FakeRpc(self.base)
        result=observe_chain(rpc,8453,self.base,NOW)
        self.assertEqual(result["status"],"OK")
        self.assertTrue(result["block_verified"])
        for p in result["pools"]:
            self.assertTrue(p["identity_verified"])
            self.assertIsNone(p["position_fees"]);self.assertIsNone(p["net_return"])
            self.assertGreater(Decimal(p["quote_per_base"]),0)
        weth=result["pools"][0]
        with localcontext() as ctx:
            ctx.prec=70
            expected=Decimal("1.0001")**(-198000)*10**12
            self.assertLess(abs(Decimal(weth["quote_per_base"])/expected-1),Decimal("1e-18"))
        aero=result["pools"][1]
        self.assertEqual(aero["fee_raw"],437)
        self.assertEqual(aero["fee_kind"],"DYNAMIC_OBSERVED_AT_BLOCK")
        self.assertTrue(all(params[1] == "0x12345" for method,params in rpc.calls if method == "eth_call"))
    def test_reversed_price_direction_for_usdg_token0(self):
        pool=POOLS[1]; x=observe_pool(FakeRpc([pool]),pool,BLOCK["number"])
        self.assertEqual(x["status"],"OK")
        self.assertEqual(x["token0"],pool["quote"])
        self.assertGreater(Decimal(x["quote_per_base"]),1)
    def test_wrong_chain_blocks_all_contract_reads(self):
        rpc=FakeRpc(self.base,chain=1);x=observe_chain(rpc,8453,self.base,NOW)
        self.assertEqual(x["status"],"ERROR");self.assertEqual(len(rpc.calls),1)
    def test_stale_block_blocks_pool_reads(self):
        rpc=FakeRpc(self.base,stale=True);x=observe_chain(rpc,8453,self.base,NOW)
        self.assertFalse(x["block_verified"]);self.assertEqual(x["pools"],[])
    def test_reorg_invalidates_every_previously_observed_price(self):
        x=observe_chain(FakeRpc(self.base,reorg=True),8453,self.base,NOW)
        self.assertFalse(x["block_verified"])
        self.assertTrue(all(p["status"] == "INVALID_BLOCK" and p["quote_per_base"] is None for p in x["pools"]))
    def test_one_failed_pool_does_not_substitute_or_erase_other_pools(self):
        x=observe_chain(FakeRpc(self.base,fail_pool=self.base[1]["id"]),8453,self.base,NOW)
        self.assertEqual(x["status"],"PARTIAL")
        self.assertEqual([p["status"] for p in x["pools"]],["OK","ERROR","OK"])
        self.assertIsNone(x["pools"][1]["quote_per_base"])
    def test_token_decimals_fee_and_uninitialized_price_fail_closed(self):
        p=self.base[0]
        for kw in (dict(wrong_tokens=True),dict(bad_decimals=True),dict(wrong_fee=True),dict(zero_price=True)):
            with self.subTest(kw=kw):
                x=observe_pool(FakeRpc([p],**kw),p,BLOCK["number"])
                self.assertEqual(x["status"],"ERROR");self.assertFalse(x["identity_verified"])
                self.assertIsNone(x["quote_per_base"])
    def test_abi_rejects_truncated_extra_noncanonical_values(self):
        for value in ("0x", "0x0", abi(1,2)):
            with self.assertRaises(ValueError):abi_words(value,1)
        self.assertEqual(int24(2**256-198000),-198000)
        with self.assertRaises(ValueError):int24(2**24-1)
    def test_read_transport_blocks_writes_unpinned_calls_and_unknown_hosts(self):
        rpc=PublicReadRpc(RPC_URLS[8453][0])
        with self.assertRaises(ValueError):rpc.request("eth_sendRawTransaction",["0x"])
        with self.assertRaises(ValueError):rpc.request("eth_call",[{"to":self.base[0]["address"],"data":SELECTORS["slot0"]},"latest"])
        with self.assertRaises(ValueError):PublicReadRpc("https://example.invalid")
    def test_complete_coverage_never_authorizes_economics_or_capital(self):
        chains=[observe_chain(FakeRpc([p for p in POOLS if p["chain"]==c]),c,[p for p in POOLS if p["chain"]==c],NOW) for c in RPC_URLS]
        x=summarize(chains)
        self.assertTrue(x["coverage_complete"]);self.assertTrue(x["synchronized_within_60s"])
        self.assertFalse(x["paper_j0_initialized"]);self.assertFalse(x["usd_peg_assumed"])
        self.assertFalse(x["automatic_capital_update_authorized"])
        chains[0]["pools"][1]["status"]="ERROR"
        self.assertFalse(summarize(chains)["coverage_complete"])
    def test_crosschain_skew_is_explicit_not_atomic(self):
        chains=[observe_chain(FakeRpc([p for p in POOLS if p["chain"]==c]),c,[p for p in POOLS if p["chain"]==c],NOW) for c in RPC_URLS]
        chains[1]["block_timestamp_utc"]="2026-10-01T00:00:00+00:00"
        x=summarize(chains)
        self.assertTrue(x["coverage_complete"]);self.assertFalse(x["synchronized_within_60s"])

if __name__ == "__main__":unittest.main()
