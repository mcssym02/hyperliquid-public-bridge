"""Bounded optional market feed; never make primary collectors depend on RPCs.

The child has a 20-second wall-clock limit. Invalid, crashed or timed-out results
replace the previous feed with explicit ERROR/nulls, never with old prices.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from collect_paper_pools import POOLS, READ_METHODS, summarize

TIMEOUT_SECONDS = 20
MAX_SOURCE_AGE_SECONDS = 180

def require(condition, message):
    if not condition:
        raise ValueError(message)

def timestamp(value):
    require(isinstance(value, str), "missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "naive timestamp")
    return parsed.timestamp()

def positive(value):
    require(isinstance(value, str), "price must retain decimal text precision")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid price") from exc
    require(number.is_finite() and number > 0, "invalid price")

def validate_snapshot(data, now=None):
    """Validate scope, usable rows, coverage and freshness before publication."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    require(isinstance(data, dict), "snapshot must be an object")
    require(data.get("service") == "exact-paper-pool-observations" and data.get("version") == "1.0.0", "wrong service/version")
    require(data.get("security") == dict(mode="READ_ONLY", transaction_broadcast=False, rpc_methods=sorted(READ_METHODS)), "wrong security scope")
    require(data.get("economic_scope") == "POOL_SPOT_ONLY", "wrong economic scope")
    for flag in ("usd_peg_assumed", "paper_j0_initialized", "automatic_capital_update_authorized"):
        require(data.get(flag) is False, "unexpected economic authorization")
    require(-30 <= now-timestamp(data.get("timestamp_utc")) <= MAX_SOURCE_AGE_SECONDS, "stale/future snapshot")
    chains = data.get("chains")
    require(isinstance(chains, list), "missing chains")
    expected = {p["id"]:p for p in POOLS}
    seen_chains, seen_pools, good, block_times = set(), set(), set(), []
    for chain in chains:
        cid = chain.get("chain_id")
        require(cid in (8453,4663) and cid not in seen_chains, "wrong/duplicate chain")
        seen_chains.add(cid)
        require(chain.get("status") in ("OK","PARTIAL","ERROR"), "invalid chain status")
        require(type(chain.get("block_verified")) is bool, "missing block verification")
        if chain["block_verified"]:
            bt = timestamp(chain.get("block_timestamp_utc"))
            require(-30 <= now-bt <= MAX_SOURCE_AGE_SECONDS, "stale/future source block")
            require(type(chain.get("block_number")) is int and chain["block_number"] >= 0, "bad block number")
            require(isinstance(chain.get("block_hash"), str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", chain["block_hash"]), "bad block hash")
            block_times.append(bt)
        rows = chain.get("pools")
        require(isinstance(rows, list), "missing pool rows")
        chain_ids = set()
        for row in rows:
            pid = row.get("pool_id")
            require(pid in expected and pid not in seen_pools, "wrong/duplicate pool")
            seen_pools.add(pid);chain_ids.add(pid)
            p = expected[pid]
            require(row.get("chain_id") == p["chain"] == cid, "pool chain mismatch")
            require(row.get("pool_address") == p["address"] and row.get("dex") == p["dex"], "pool identity mismatch")
            require(row.get("price_unit") == p["unit"], "wrong quote units")
            for field in ("volume_24h", "tvl_usd", "position_fees", "net_return"):
                require(field in row and row[field] is None, "unsupported economic inference")
            if row.get("status") == "OK":
                require(chain["block_verified"] and chain["status"] != "ERROR" and row.get("identity_verified") is True, "unverified usable price")
                tokens = sorted(p["decimals"])
                require([row.get("token0"),row.get("token1")] == tokens, "token mismatch")
                require(row.get("token_decimals") == [p["decimals"][t] for t in tokens], "decimal mismatch")
                positive(row.get("quote_per_base"))
                require(type(row.get("fee_raw")) is int and 0 <= row["fee_raw"] < 1_000_000 and row.get("fee_denominator") == 1_000_000, "bad fee")
                require(type(row.get("tick_spacing")) is int and 0 < row["tick_spacing"] <= 16384, "bad spacing")
                if p["fee"] is not None:
                    require((row["fee_raw"],row["tick_spacing"]) == (p["fee"],p["spacing"]), "fixed tier mismatch")
                good.add(pid)
            else:
                require(row.get("status") in ("ERROR","INVALID_BLOCK") and row.get("identity_verified") is False and row.get("quote_per_base") is None, "unavailable row retained a price")
        if chain["status"] == "OK":
            require(chain["block_verified"] and chain_ids == {p["id"] for p in POOLS if p["chain"] == cid} and chain_ids <= good, "incomplete chain claims OK")
    complete = (good == set(expected) and seen_chains == {8453,4663} and all(c["status"] == "OK" for c in chains))
    require(data.get("coverage_complete") is complete, "coverage mismatch")
    require(type(data.get("verified_pool_count")) is int and data["verified_pool_count"] == len(good) and data.get("expected_pool_count") == len(POOLS), "pool count mismatch")
    expected_status = "OK" if complete else "PARTIAL" if good else "ERROR"
    require(data.get("status") == expected_status, "status mismatch")
    skew = max(block_times)-min(block_times) if len(block_times) == 2 else None
    require(data.get("cross_chain_timestamp_skew_seconds") == skew, "skew mismatch")
    require(data.get("synchronized_within_60s") is (complete and skew is not None and skew <= 60), "synchronization mismatch")
    return data

def refresh(output, runner=subprocess.run, now=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc).isoformat()
    reason = None
    try:
        with tempfile.TemporaryDirectory(prefix="paper-pool-", dir=output.parent) as work:
            child_output = Path(work)/"snapshot.json"
            result = runner([sys.executable, str(Path(__file__).with_name("collect_paper_pools.py")), "--output", str(child_output)],
                timeout=TIMEOUT_SECONDS, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            require(result.returncode in (0,2), "COLLECTOR_PROCESS_FAILED")
            data = validate_snapshot(json.loads(child_output.read_text()), now=now)
            require(result.returncode == (0 if data["coverage_complete"] else 2), "COLLECTOR_EXIT_STATUS_MISMATCH")
    except subprocess.TimeoutExpired:
        reason = "COLLECTOR_TIMEOUT_20S"
    except Exception as exc:
        reason = "COLLECTOR_INVALID_OR_UNAVAILABLE:" + type(exc).__name__
    if reason:
        data = summarize([])
        data["errors"] = [reason]
    data["refresh"] = dict(wall_timeout_seconds=TIMEOUT_SECONDS, started_utc=start,
        finished_utc=datetime.now(timezone.utc).isoformat(), previous_prices_reused=False,
        primary_market_dependency=False)
    temporary = output.with_suffix(output.suffix+".tmp")
    temporary.write_text(json.dumps(data, indent=2)+"\n")
    temporary.replace(output)
    return data

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = refresh(args.output)
    print(json.dumps({k:data[k] for k in ("status","coverage_complete","verified_pool_count")}))
    # Source degradation is data, not a reason to stop publishing primary markets.
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
