from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

import httpx

DATA_DIR = Path("data")
OUTPUT_FILE = DATA_DIR / "uniswap-lp.json"

CHAIN_ID = 8453
CHAIN_NAME = "Base"
POSITION_MANAGER = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
NFT_ID = 6000457
EXPECTED_OWNER = "0xf6c62b83f742246080bb42a3e2512474bc690ed1"

RPC_URLS = (
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
)
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

SELECTOR_POSITIONS = "99fbab88"
SELECTOR_OWNER_OF = "6352211e"
SELECTOR_GET_POOL = "1698ee82"
SELECTOR_SLOT0 = "3850c7bd"
SELECTOR_DECIMALS = "313ce567"
SELECTOR_SYMBOL = "95d89b41"
SELECTOR_COLLECT = "fc6f7865"

MAX_UINT128 = (1 << 128) - 1
Q96 = Decimal(2**96)
getcontext().prec = 80

KNOWN_TOKENS = {
    "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "decimals": 18},
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "decimals": 6},
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(OUTPUT_FILE)


def strip_0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


def word_uint(value: int) -> str:
    return f"{value:064x}"


def word_address(address: str) -> str:
    raw = strip_0x(address).lower()
    if len(raw) != 40:
        raise ValueError(f"invalid address: {address}")
    return ("0" * 24) + raw


def decode_words(hex_data: str) -> list[bytes]:
    raw = bytes.fromhex(strip_0x(hex_data))
    if len(raw) % 32:
        raise ValueError(f"ABI result is not word-aligned: {len(raw)} bytes")
    return [raw[i : i + 32] for i in range(0, len(raw), 32)]


def decode_address(word: bytes) -> str:
    return "0x" + word[-20:].hex()


def decode_uint(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=False)


def decode_int(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def decode_symbol(result: str) -> str | None:
    raw = bytes.fromhex(strip_0x(result))
    if not raw:
        return None
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            size = int.from_bytes(raw[offset : offset + 32], "big")
            start = offset + 32
            end = start + size
            if 0 <= size <= 128 and end <= len(raw):
                try:
                    return raw[start:end].decode("utf-8").strip("\x00")
                except UnicodeDecodeError:
                    pass
    try:
        return raw[:32].rstrip(b"\x00").decode("utf-8")
    except UnicodeDecodeError:
        return None


class Rpc:
    def __init__(self) -> None:
        self.url: str | None = None
        self.client = httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "hyperliquid-public-bridge/1.0 uniswap-base-readonly"},
        )
        self.counter = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def request_at(self, url: str, method: str, params: list[Any]) -> Any:
        self.counter += 1
        response = await self.client.post(
            url,
            json={"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"RPC {method} error: {payload['error']}")
        return payload.get("result")

    async def connect(self) -> str:
        errors: list[str] = []
        for url in RPC_URLS:
            try:
                chain_hex = await self.request_at(url, "eth_chainId", [])
                if int(chain_hex, 16) != CHAIN_ID:
                    raise RuntimeError(f"unexpected chain id {chain_hex}")
                self.url = url
                return url
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {str(exc)[:160]}")
        raise RuntimeError("No Base RPC available: " + " | ".join(errors))

    async def eth_call(self, to: str, data: str, from_address: str | None = None) -> str:
        if not self.url:
            raise RuntimeError("RPC not connected")
        tx: dict[str, str] = {"to": to, "data": data}
        if from_address:
            tx["from"] = from_address
        result = await self.request_at(self.url, "eth_call", [tx, "latest"])
        if not isinstance(result, str) or result == "0x":
            raise RuntimeError(f"empty eth_call result for {to} {data[:10]}")
        return result


async def token_metadata(rpc: Rpc, address: str) -> dict[str, Any]:
    key = address.lower()
    known = KNOWN_TOKENS.get(key, {})
    decimals = known.get("decimals")
    symbol = known.get("symbol")
    errors: list[str] = []

    if decimals is None:
        try:
            words = decode_words(await rpc.eth_call(address, "0x" + SELECTOR_DECIMALS))
            decimals = decode_uint(words[0])
        except Exception as exc:
            errors.append(f"decimals: {type(exc).__name__}: {str(exc)[:120]}")

    if symbol is None:
        try:
            symbol = decode_symbol(await rpc.eth_call(address, "0x" + SELECTOR_SYMBOL))
        except Exception as exc:
            errors.append(f"symbol: {type(exc).__name__}: {str(exc)[:120]}")

    return {
        "address": address,
        "symbol": symbol or "UNKNOWN",
        "decimals": decimals,
        "metadata_status": "OK" if decimals is not None else "PARTIAL",
        "errors": errors,
    }


def human_amount(raw: Decimal, decimals: int | None) -> float | None:
    if decimals is None:
        return None
    return float(raw / (Decimal(10) ** decimals))


def price_token1_per_token0_from_sqrt(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Decimal:
    ratio = Decimal(sqrt_price_x96) / Q96
    return ratio * ratio * (Decimal(10) ** (decimals0 - decimals1))


def tick_price_token1_per_token0(tick: int, decimals0: int, decimals1: int) -> Decimal:
    raw = Decimal(str(math.pow(1.0001, tick)))
    return raw * (Decimal(10) ** (decimals0 - decimals1))


def principal_raw_amounts(
    liquidity: int,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
) -> tuple[Decimal, Decimal]:
    L = Decimal(liquidity)
    sqrt_p = Decimal(sqrt_price_x96) / Q96
    sqrt_a = Decimal(str(math.pow(1.0001, tick_lower / 2.0)))
    sqrt_b = Decimal(str(math.pow(1.0001, tick_upper / 2.0)))

    if sqrt_p <= sqrt_a:
        amount0 = L * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        amount1 = Decimal(0)
    elif sqrt_p < sqrt_b:
        amount0 = L * (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
        amount1 = L * (sqrt_p - sqrt_a)
    else:
        amount0 = Decimal(0)
        amount1 = L * (sqrt_b - sqrt_a)
    return amount0, amount1


def classify_range(current_tick: int, tick_lower: int, tick_upper: int) -> str:
    if current_tick < tick_lower:
        return "OUT OF RANGE — BAS"
    if current_tick >= tick_upper:
        return "OUT OF RANGE — HAUT"
    return "IN RANGE"


async def collect_fee_quote(
    rpc: Rpc,
    owner: str,
    decimals0: int | None,
    decimals1: int | None,
) -> dict[str, Any]:
    calldata = (
        "0x"
        + SELECTOR_COLLECT
        + word_uint(NFT_ID)
        + word_address(owner)
        + word_uint(MAX_UINT128)
        + word_uint(MAX_UINT128)
    )
    try:
        result = await rpc.eth_call(POSITION_MANAGER, calldata, from_address=owner)
        words = decode_words(result)
        if len(words) < 2:
            raise RuntimeError("collect quote returned fewer than 2 words")
        raw0 = decode_uint(words[0])
        raw1 = decode_uint(words[1])
        return {
            "status": "OK",
            "method": "eth_call simulation of NonfungiblePositionManager.collect; no state change, no signature, no broadcast",
            "token0_raw": str(raw0),
            "token1_raw": str(raw1),
            "token0": human_amount(Decimal(raw0), decimals0),
            "token1": human_amount(Decimal(raw1), decimals1),
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "method": "eth_call simulation of NonfungiblePositionManager.collect",
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def stable_value_usdc(
    symbol0: str,
    symbol1: str,
    amount0: float | None,
    amount1: float | None,
    price1_per_0: float | None,
) -> float | None:
    if amount0 is None or amount1 is None or price1_per_0 is None:
        return None
    if symbol1.upper() == "USDC":
        return amount0 * price1_per_0 + amount1
    if symbol0.upper() == "USDC" and price1_per_0 != 0:
        return amount0 + amount1 / price1_per_0
    return None


async def collect() -> dict[str, Any]:
    timestamp = iso_now()
    rpc = Rpc()
    try:
        rpc_url = await rpc.connect()

        owner_data = "0x" + SELECTOR_OWNER_OF + word_uint(NFT_ID)
        owner_words = decode_words(await rpc.eth_call(POSITION_MANAGER, owner_data))
        owner = decode_address(owner_words[0])

        position_data = "0x" + SELECTOR_POSITIONS + word_uint(NFT_ID)
        words = decode_words(await rpc.eth_call(POSITION_MANAGER, position_data))
        if len(words) < 12:
            raise RuntimeError(f"positions() returned {len(words)} words, expected 12")

        token0 = decode_address(words[2])
        token1 = decode_address(words[3])
        fee = decode_uint(words[4])
        tick_lower = decode_int(words[5])
        tick_upper = decode_int(words[6])
        liquidity = decode_uint(words[7])
        tokens_owed0_stored = decode_uint(words[10])
        tokens_owed1_stored = decode_uint(words[11])

        pool_data = (
            "0x"
            + SELECTOR_GET_POOL
            + word_address(token0)
            + word_address(token1)
            + word_uint(fee)
        )
        pool_words = decode_words(await rpc.eth_call(FACTORY, pool_data))
        pool = decode_address(pool_words[0])
        if int(strip_0x(pool), 16) == 0:
            raise RuntimeError("factory returned zero pool address")

        slot0_words = decode_words(await rpc.eth_call(pool, "0x" + SELECTOR_SLOT0))
        if len(slot0_words) < 2:
            raise RuntimeError("slot0() returned fewer than 2 words")
        sqrt_price_x96 = decode_uint(slot0_words[0])
        current_tick = decode_int(slot0_words[1])

        meta0, meta1 = await asyncio.gather(
            token_metadata(rpc, token0),
            token_metadata(rpc, token1),
        )
        decimals0 = meta0.get("decimals")
        decimals1 = meta1.get("decimals")
        if not isinstance(decimals0, int) or not isinstance(decimals1, int):
            raise RuntimeError("token decimals unavailable")

        price_dec = price_token1_per_token0_from_sqrt(sqrt_price_x96, decimals0, decimals1)
        lower_dec = tick_price_token1_per_token0(tick_lower, decimals0, decimals1)
        upper_dec = tick_price_token1_per_token0(tick_upper, decimals0, decimals1)

        raw0, raw1 = principal_raw_amounts(liquidity, sqrt_price_x96, tick_lower, tick_upper)
        amount0 = human_amount(raw0, decimals0)
        amount1 = human_amount(raw1, decimals1)

        fees = await collect_fee_quote(rpc, owner, decimals0, decimals1)
        fee0 = fees.get("token0") if fees.get("status") == "OK" else None
        fee1 = fees.get("token1") if fees.get("status") == "OK" else None

        price = float(price_dec)
        principal_usdc = stable_value_usdc(
            str(meta0.get("symbol")), str(meta1.get("symbol")), amount0, amount1, price
        )
        fees_usdc = stable_value_usdc(
            str(meta0.get("symbol")), str(meta1.get("symbol")), fee0, fee1, price
        )
        total_usdc = (
            principal_usdc + fees_usdc
            if principal_usdc is not None and fees_usdc is not None
            else principal_usdc
        )

        owner_matches = owner.lower() == EXPECTED_OWNER.lower()
        range_status = classify_range(current_tick, tick_lower, tick_upper)

        return {
            "service": "uniswap-v3-base-lp-readonly",
            "version": "1.0.0",
            "timestamp_utc": timestamp,
            "status": "OK" if fees.get("status") == "OK" else "PARTIAL",
            "security": {
                "mode": "READ_ONLY",
                "private_key": False,
                "wallet_connection": False,
                "signature": False,
                "transaction_broadcast": False,
                "rpc_methods": ["eth_chainId", "eth_call"],
            },
            "chain": {"name": CHAIN_NAME, "chain_id": CHAIN_ID, "rpc_url": rpc_url},
            "contracts": {
                "nonfungible_position_manager": POSITION_MANAGER,
                "factory": FACTORY,
                "pool": pool,
            },
            "position": {
                "nft_id": NFT_ID,
                "expected_owner": EXPECTED_OWNER,
                "owner_onchain": owner,
                "owner_matches_expected": owner_matches,
                "token0": meta0,
                "token1": meta1,
                "fee_tier_raw": fee,
                "fee_tier_pct": fee / 1_000_000.0 * 100.0,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
                "liquidity": str(liquidity),
                "tokens_owed_stored_raw": {
                    "token0": str(tokens_owed0_stored),
                    "token1": str(tokens_owed1_stored),
                    "note": "Storage snapshot only; live collectable fees are quoted separately via eth_call collect simulation.",
                },
            },
            "pool_state": {
                "sqrt_price_x96": str(sqrt_price_x96),
                "current_tick": current_tick,
                "range_status": range_status,
                "price_token1_per_token0": price,
                "range_lower_token1_per_token0": float(lower_dec),
                "range_upper_token1_per_token0": float(upper_dec),
                "price_orientation": f"{meta1.get('symbol')} per {meta0.get('symbol')}",
            },
            "principal": {
                "token0": amount0,
                "token1": amount1,
                "token0_symbol": meta0.get("symbol"),
                "token1_symbol": meta1.get("symbol"),
                "estimated_value_usdc": principal_usdc,
                "method": "Uniswap V3 liquidity math from on-chain liquidity, slot0 sqrtPriceX96, and position ticks.",
            },
            "fees_collectable": {
                **fees,
                "token0_symbol": meta0.get("symbol"),
                "token1_symbol": meta1.get("symbol"),
                "estimated_value_usdc": fees_usdc,
            },
            "estimated_total_value_usdc": total_usdc,
            "state_guard": {
                "confirmed_onchain_position": owner_matches,
                "safe_for_automatic_state_update": bool(owner_matches and range_status in {
                    "IN RANGE", "OUT OF RANGE — HAUT", "OUT OF RANGE — BAS"
                }),
                "rule": "If owner/NFT identity no longer matches expected state, do not reuse this NFT's range as the active LP.",
            },
            "sources": {
                "position_owner_range_liquidity": "Base JSON-RPC eth_call -> official Uniswap V3 NonfungiblePositionManager",
                "pool_price_tick": "Base JSON-RPC eth_call -> Uniswap V3 pool slot0",
                "fees": "Base JSON-RPC eth_call simulation -> NonfungiblePositionManager.collect; simulation discarded",
                "deployment_addresses": "Uniswap official contracts deployment registry for Base chain 8453",
            },
        }
    except Exception as exc:
        return {
            "service": "uniswap-v3-base-lp-readonly",
            "version": "1.0.0",
            "timestamp_utc": timestamp,
            "status": "ERROR",
            "security": {
                "mode": "READ_ONLY",
                "private_key": False,
                "wallet_connection": False,
                "signature": False,
                "transaction_broadcast": False,
            },
            "position": {
                "nft_id": NFT_ID,
                "expected_owner": EXPECTED_OWNER,
            },
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
    finally:
        await rpc.close()


async def main() -> None:
    payload = await collect()
    write_json(payload)
    print(json.dumps({
        "service": payload.get("service"),
        "status": payload.get("status"),
        "timestamp_utc": payload.get("timestamp_utc"),
        "nft_id": payload.get("position", {}).get("nft_id"),
        "owner_matches_expected": payload.get("position", {}).get("owner_matches_expected"),
        "range_status": payload.get("pool_state", {}).get("range_status"),
        "estimated_total_value_usdc": payload.get("estimated_total_value_usdc"),
        "fees_status": payload.get("fees_collectable", {}).get("status"),
    }))


if __name__ == "__main__":
    asyncio.run(main())
