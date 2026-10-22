"""Inventory directly owned Uniswap V3 Base NFTs; never change the tracked NFT.

Only public RPC reads. All state calls are pinned to one block. This scope excludes
other chains, managers, accounts and NFTs held by staking/custody contracts.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from lp_state import classify_position

MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
RPC_URLS = ("https://mainnet.base.org", "https://base-rpc.publicnode.com", "https://base.llamarpc.com")
READ_METHODS = {"eth_chainId", "eth_blockNumber", "eth_call"}

def address(value: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("invalid owner address")
    return value.lower()

def word(value: int) -> str:
    if value < 0 or value >= 2 ** 256:
        raise ValueError("invalid uint256")
    return f"{value:064x}"

def words(value: str, minimum: int = 1) -> list[int]:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid ABI result")
    payload = value[2:]
    if len(payload) % 64 or len(payload) < minimum * 64:
        raise ValueError("short or unaligned ABI result")
    return [int(payload[i:i+64], 16) for i in range(0, len(payload), 64)]

def addr_word(value: int) -> str:
    if value >= 2 ** 160:
        raise ValueError("noncanonical address word")
    return "0x" + f"{value:040x}"

class PublicRpc:
    def __init__(self, url: str, timeout: float = 12):
        self.url, self.timeout, self.counter = url, timeout, 0

    def request(self, method: str, params: list):
        if method not in READ_METHODS:
            raise ValueError("RPC method forbidden")
        self.counter += 1
        req = urllib.request.Request(self.url,
            data=json.dumps({"jsonrpc":"2.0", "id":self.counter,
                             "method":method, "params":params}).encode(),
            headers={"Content-Type":"application/json", "User-Agent":"public-lp-inventory/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            data = json.load(response)
        if data.get("error") or "result" not in data or data.get("id") != self.counter:
            raise RuntimeError("invalid public RPC response")
        return data["result"]

def inventory(rpc, owner: str, max_positions: int = 250) -> dict:
    owner = address(owner)
    if max_positions < 1:
        raise ValueError("max_positions must be positive")
    result = {
        "service":"uniswap-v3-base-owned-nft-inventory", "version":"1.0.0",
        "timestamp_utc":datetime.now(timezone.utc).isoformat(), "status":"ERROR",
        "security":{"mode":"READ_ONLY", "transaction_broadcast":False,
                    "rpc_methods":sorted(READ_METHODS)},
        "scope":{"chain_id":8453, "manager":MANAGER, "owner":owner,
                 "direct_ownership_only":True,
                 "excludes":["other_chains", "other_managers", "other_wallets", "custody_or_staking_contracts"]},
        "positions":[], "errors":[], "coverage_complete":False,
        "active_nft_ids":None, "tracked_nft_change":False,
        "automatic_capital_update_authorized":False,
    }
    try:
        if int(rpc.request("eth_chainId", []), 16) != 8453:
            raise ValueError("wrong chain")
        block = rpc.request("eth_blockNumber", [])
        if not re.fullmatch(r"0x[0-9a-fA-F]+", block):
            raise ValueError("invalid block number")
        result["block_number"] = int(block, 16)
        def call(data: str, minimum: int = 1):
            return words(rpc.request("eth_call", [{"to":MANAGER,"data":"0x"+data},block]), minimum)
        owner_arg = owner[2:].rjust(64, "0")
        count = call("70a08231" + owner_arg)[0]
        result["owned_nft_count"] = count
        if count > max_positions:
            result["errors"].append("POSITION_LIMIT_EXCEEDED: inventory incomplete")
        seen = set()
        for index in range(min(count, max_positions)):
            try:
                nft = call("2f745c59" + owner_arg + word(index))[0]
                if nft in seen:
                    raise ValueError("duplicate enumerated NFT")
                seen.add(nft)
                actual_owner = addr_word(call("6352211e" + word(nft))[0])
                if actual_owner != owner:
                    raise ValueError("NFT owner mismatch")
                p = call("99fbab88" + word(nft), 12)
                tick = lambda n: (n & ((1 << 24)-1)) - (1 << 24) if n & (1 << 23) else n & ((1 << 24)-1)
                result["positions"].append({"nft_id":nft,"owner_verified":True,
                    "token0":addr_word(p[2]),"token1":addr_word(p[3]),"fee_tier_raw":p[4],
                    "tick_lower":tick(p[5]),"tick_upper":tick(p[6]),
                    "liquidity":str(p[7]),"tokens_owed_stored_raw":[str(p[10]),str(p[11])],
                    **classify_position(p[7], p[10], p[11])})
            except Exception as exc:
                result["errors"].append(f"index={index}: {type(exc).__name__}: {exc}")
        result["coverage_complete"] = not result["errors"] and len(result["positions"]) == count
        # Null is deliberate: partial enumeration must not assert an exhaustive list.
        if result["coverage_complete"]:
            result["active_nft_ids"] = [p["nft_id"] for p in result["positions"] if p["has_active_liquidity"]]
        result["observed_active_nft_ids"] = [p["nft_id"] for p in result["positions"] if p["has_active_liquidity"]]
        result["status"] = "OK" if result["coverage_complete"] else "PARTIAL"
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=os.environ.get("UNISWAP_EXPECTED_OWNER"))
    parser.add_argument("--max-positions", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.owner:
        parser.error("--owner or UNISWAP_EXPECTED_OWNER is required")
    results = []
    for endpoint in RPC_URLS:
        result = inventory(PublicRpc(endpoint), args.owner, args.max_positions)
        result["rpc_url"] = endpoint
        results.append(result)
        if result["status"] == "OK":
            break
    result = next((r for r in reversed(results) if r["status"] != "ERROR"), results[-1])
    result["provider_attempt_count"] = len(results)
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_suffix(args.output.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(args.output)
    else:
        print(payload, end="")
    return 0 if result["status"] == "OK" else 2

if __name__ == "__main__":
    raise SystemExit(main())
