"""Observe five exact PAPER pools using public reads, without valuing a position.

No J0, fees, USD peg, wallet state or historical performance is inferred here.
Each chain has its own pinned block; the two chains are not atomic.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import time
import urllib.request

READ_METHODS = {"eth_chainId", "eth_getBlockByNumber", "eth_call"}
SELECTORS = {"token0":"0x0dfe1681", "token1":"0xd21220a7",
             "decimals":"0x313ce567", "fee":"0xddca3f43",
             "tickSpacing":"0xd0c93a7c", "slot0":"0x3850c7bd",
             "liquidity":"0x1a686502"}
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NVDA = "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
QQQ = "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
POOLS = (
    dict(id="BASE_UNIV3_WETH_USDC", chain=8453, dex="uniswap-v3",
         address="0x6c561b446416e1a00e8e93e221854d6ea4171372",
         base=WETH, quote=USDC, decimals={WETH:18, USDC:6}, unit="USDC/WETH", fee=3000, spacing=60),
    dict(id="RH_UNIV3_NVDA_USDG", chain=4663, dex="uniswap-v3",
         address="0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3",
         base=NVDA, quote=USDG, decimals={NVDA:18, USDG:6}, unit="USDG/NVDA", fee=500, spacing=10),
    dict(id="RH_UNIV3_QQQ_USDG", chain=4663, dex="uniswap-v3",
         address="0xd60a5d14db690b7afad71f76b108071d7175597d",
         base=QQQ, quote=USDG, decimals={QQQ:18, USDG:6}, unit="USDG/QQQ", fee=500, spacing=10),
    dict(id="BASE_SLIPSTREAM_WETH_USDC", chain=8453, dex="aerodrome-slipstream",
         address="0x3fe04a59ebd38cf06080a6f60a98d124eb59392a",
         base=WETH, quote=USDC, decimals={WETH:18, USDC:6}, unit="USDC/WETH", fee=None, spacing=None),
    dict(id="BASE_UNIV3_CBBTC_WETH", chain=8453, dex="uniswap-v3",
         address="0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
         base=CBBTC, quote=WETH, decimals={CBBTC:8, WETH:18}, unit="WETH/cbBTC", fee=500, spacing=10),
)
RPC_URLS = {8453:("https://base-rpc.publicnode.com", "https://mainnet.base.org"),
            4663:("https://rpc.mainnet.chain.robinhood.com",)}

def hex_quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise ValueError("invalid RPC quantity")
    return int(value, 16)

def abi_words(value, count):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})+", value):
        raise ValueError("invalid ABI result")
    result = [int(value[i:i+64], 16) for i in range(2, len(value), 64)]
    if len(result) != count:
        raise ValueError("unexpected ABI word count")
    return result

def uint(value, bits):
    if not 0 <= value < 2**bits:
        raise ValueError("noncanonical uint")
    return value

def int24(value):
    signed = value - 2**256 if value >= 2**255 else value
    if not -(2**23) <= signed < 2**23:
        raise ValueError("noncanonical int24")
    return signed

def address_word(value):
    return "0x" + f"{uint(value, 160):040x}"

class PublicReadRpc:
    def __init__(self, url, budget_seconds=85, timeout=6):
        if url not in {u for urls in RPC_URLS.values() for u in urls}:
            raise ValueError("RPC endpoint not allowlisted")
        self.url, self.timeout, self.counter = url, timeout, 0
        self.deadline = time.monotonic() + budget_seconds

    def request(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("RPC method forbidden")
        if method == "eth_call" and (set(params[0]) != {"to", "data"} or
                params[0]["data"] not in SELECTORS.values() or params[1] == "latest"):
            raise ValueError("only pinned allowlisted getter calls are permitted")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("chain read budget exhausted")
        self.counter += 1
        request = urllib.request.Request(self.url,
            data=json.dumps(dict(jsonrpc="2.0", id=self.counter, method=method, params=params)).encode(),
            headers={"Content-Type":"application/json", "User-Agent":"paper-pool-observer/1.0"})
        with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or payload.get("id") != self.counter or payload.get("error") or "result" not in payload:
            raise RuntimeError("invalid RPC response")
        return payload["result"]

def block_header(value):
    number, timestamp = hex_quantity(value["number"]), hex_quantity(value["timestamp"])
    block_hash = value["hash"]
    if not isinstance(block_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash):
        raise ValueError("invalid block hash")
    return number, timestamp, block_hash.lower()

def observe_pool(rpc, pool, block):
    result = dict(pool_id=pool["id"], chain_id=pool["chain"], dex=pool["dex"],
        pool_address=pool["address"], status="ERROR", identity_verified=False,
        quote_per_base=None, price_unit=pool["unit"], errors=[],
        volume_24h=None, tvl_usd=None, position_fees=None, net_return=None)
    try:
        def call(target, field, count=1):
            return abi_words(rpc.request("eth_call", [{"to":target, "data":SELECTORS[field]}, block]), count)
        t0 = address_word(call(pool["address"], "token0")[0])
        t1 = address_word(call(pool["address"], "token1")[0])
        if [t0, t1] != sorted(pool["decimals"]):
            raise ValueError("pool token identity/order mismatch")
        decimals = [uint(call(t, "decimals")[0], 8) for t in (t0, t1)]
        if decimals != [pool["decimals"][t] for t in (t0, t1)]:
            raise ValueError("token decimals mismatch")
        fee = uint(call(pool["address"], "fee")[0], 24)
        spacing = int24(call(pool["address"], "tickSpacing")[0])
        if not 0 <= fee < 1_000_000 or spacing <= 0 or spacing > 16384:
            raise ValueError("invalid fee/tick spacing")
        if pool["fee"] is not None and (fee, spacing) != (pool["fee"], pool["spacing"]):
            raise ValueError("fee tier/tick spacing mismatch")
        slot = call(pool["address"], "slot0", 6 if pool["dex"] == "aerodrome-slipstream" else 7)
        sqrt_price = uint(slot[0], 160)
        tick = int24(slot[1])
        if not 4295128739 <= sqrt_price < 1461446703485210103287273052203988822378723970342 or not -887272 <= tick < 887272:
            raise ValueError("uninitialized/out-of-bounds price or tick")
        liquidity = uint(call(pool["address"], "liquidity")[0], 128)
        with localcontext() as ctx:
            ctx.prec = 70
            raw_ratio = (Decimal(sqrt_price) / Decimal(2**96))**2
            # slot0.tick may be one tick below the sqrt-implied tick at a boundary.
            lower = Decimal("1.0001")**(Decimal(tick)/2) * Decimal(2**96)
            upper = Decimal("1.0001")**(Decimal(tick+1)/2) * Decimal(2**96)
            if not lower-1 <= sqrt_price <= upper+1:
                raise ValueError("sqrt price/tick inconsistency")
            ratio = raw_ratio * Decimal(10)**(decimals[0]-decimals[1])
            price = ratio if pool["base"] == t0 else 1/ratio
            result.update(quote_per_base=str(price), token0=t0, token1=t1, token_decimals=decimals,
                sqrt_price_x96=str(sqrt_price), tick=tick, active_liquidity_raw=str(liquidity),
                fee_raw=fee, fee_denominator=1_000_000, tick_spacing=spacing,
                fee_kind="DYNAMIC_OBSERVED_AT_BLOCK" if pool["fee"] is None else "FIXED_TIER",
                status="OK", identity_verified=True, tradeable_depth_verified=False,
                quality="OBSERVED_ONCHAIN_SPOT_NOT_USD_OR_EXECUTION_QUOTE")
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result

def observe_chain(rpc, chain, pools, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    result = dict(chain_id=chain, status="ERROR", block_verified=False, pools=[], errors=[])
    try:
        if hex_quantity(rpc.request("eth_chainId", [])) != chain:
            raise ValueError("wrong chain")
        number, timestamp, block_hash = block_header(rpc.request("eth_getBlockByNumber", ["latest", False]))
        age = now-timestamp
        if not -30 <= age <= 180:
            raise ValueError("stale/future block timestamp")
        block = hex(number)
        result.update(block_number=number, block_hash=block_hash, block_timestamp_utc=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), block_age_seconds=age)
        result["pools"] = [observe_pool(rpc, p, block) for p in pools]
        last = block_header(rpc.request("eth_getBlockByNumber", [block, False]))
        if last != (number, timestamp, block_hash):
            raise ValueError("pinned block changed during collection")
        result["block_verified"] = True
        result["status"] = "OK" if all(p["status"] == "OK" for p in result["pools"]) else "PARTIAL"
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        # A reorg/failed final header invalidates every price from this chain.
        for p in result["pools"]:
            p.update(status="INVALID_BLOCK", identity_verified=False, quote_per_base=None)
    return result

def summarize(chains):
    prices = [p for c in chains for p in c["pools"] if p["status"] == "OK" and c["block_verified"]]
    timestamps = [datetime.fromisoformat(c["block_timestamp_utc"]).timestamp() for c in chains if c["block_verified"]]
    skew = max(timestamps)-min(timestamps) if len(timestamps) == 2 else None
    complete = (len(prices) == len(POOLS) and {p["pool_id"] for p in prices} == {p["id"] for p in POOLS}
                and {c["chain_id"] for c in chains} == set(RPC_URLS) and all(c["status"] == "OK" for c in chains))
    return dict(service="exact-paper-pool-observations", version="1.0.0", timestamp_utc=datetime.now(timezone.utc).isoformat(),
        status="OK" if complete else "PARTIAL" if prices else "ERROR", coverage_complete=complete,
        verified_pool_count=len(prices), expected_pool_count=len(POOLS), chains=chains,
        cross_chain_timestamp_skew_seconds=skew, synchronized_within_60s=complete and skew is not None and skew <= 60,
        security=dict(mode="READ_ONLY", transaction_broadcast=False, rpc_methods=sorted(READ_METHODS)),
        economic_scope="POOL_SPOT_ONLY", usd_peg_assumed=False, paper_j0_initialized=False,
        automatic_capital_update_authorized=False,
        unavailable=["position_fees", "rewards", "costs", "net_return", "volume_24h", "tvl_usd", "time_in_range", "usd_prices"],
        limitations=["Separate pinned blocks per chain; not atomic or finalized", "Spot price is not a trade execution quote or independent oracle", "Raw active liquidity is not TVL or position fees", "No historical gap or J0 is reconstructed"])

def collect():
    def one(chain):
        pools = [p for p in POOLS if p["chain"] == chain]
        failures = []
        for url in RPC_URLS[chain]:
            result = observe_chain(PublicReadRpc(url), chain, pools)
            result["rpc_url"] = url
            if result["status"] != "ERROR":
                break
            failures.append(dict(rpc_url=url, errors=result["errors"]))
        result["provider_failures"] = failures
        return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        return summarize(list(executor.map(one, RPC_URLS)))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps({k:result[k] for k in ("status", "verified_pool_count", "expected_pool_count", "synchronized_within_60s")}))
    return 0 if result["coverage_complete"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
