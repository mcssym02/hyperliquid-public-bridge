from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


API = "https://api.geckoterminal.com/api/v2"
OUTPUT = Path("data/uniswap-universe.json")
HEADERS = {"Accept": "application/json;version=20230203", "User-Agent": "public-uniswap-research/1.0"}
NETWORK = "base"
DEX = "uniswap-v3-base"
PAGES = 4
MIN_LIQUIDITY_USD = 100_000.0
MIN_VOLUME_24H_USD = 100_000.0
ALLOWED_QUOTES = {"USDC", "USDT", "WETH", "ETH", "CBBTC", "WBTC"}


def number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def main() -> None:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS, follow_redirects=True) as client:
        for page in range(1, PAGES + 1):
            try:
                response = await client.get(f"{API}/networks/{NETWORK}/dexes/{DEX}/pools", params={"page": page})
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("data", []):
                    attr = item.get("attributes", {})
                    relationships = item.get("relationships", {})
                    liquidity = number(attr.get("reserve_in_usd"))
                    volume = number((attr.get("volume_usd") or {}).get("h24"))
                    if liquidity is None or volume is None or liquidity < MIN_LIQUIDITY_USD or volume < MIN_VOLUME_24H_USD:
                        continue
                    name = str(attr.get("name") or "")
                    symbols = {p.strip().upper() for p in name.replace("/", " ").split()}
                    if not symbols.intersection(ALLOWED_QUOTES):
                        continue
                    pool_address = str(attr.get("address") or item.get("id", "").split("_")[-1])
                    fee_match = re.search(r"(\d+(?:\.\d+)?)\s*%", name)
                    fee_pct = number(fee_match.group(1)) if fee_match else None
                    gross_fees_24h = volume * fee_pct / 100.0 if fee_pct is not None else None
                    fee_to_tvl_daily = gross_fees_24h / liquidity if gross_fees_24h is not None and liquidity else None
                    rows.append({
                        "network": NETWORK,
                        "dex": DEX,
                        "pool_address": pool_address,
                        "name": name,
                        "liquidity_usd": liquidity,
                        "volume_24h_usd": volume,
                        "transactions_24h": (attr.get("transactions") or {}).get("h24"),
                        "price_change_24h_pct": number((attr.get("price_change_percentage") or {}).get("h24")),
                        "fee_pct_if_parsed": fee_pct,
                        "gross_fees_24h_if_parsed": gross_fees_24h,
                        "gross_fee_to_tvl_daily_if_parsed": fee_to_tvl_daily,
                        "pool_created_at": attr.get("pool_created_at"),
                        "relationships": relationships,
                    })
            except Exception as exc:
                errors.append(f"page {page}: {type(exc).__name__}: {str(exc)[:180]}")
            if page != PAGES:
                await asyncio.sleep(6.2)

    rows.sort(key=lambda r: (r.get("gross_fee_to_tvl_daily_if_parsed") or -1, r.get("volume_24h_usd") or 0), reverse=True)
    result = {
        "service": "uniswap-opportunity-universe",
        "version": "1.0.0",
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "OK" if rows and not errors else ("PARTIAL" if rows else "ERROR"),
        "scope": {"network": NETWORK, "dex": DEX, "pages_scanned": PAGES, "note": "Top pools exposed by the public API, not every historical pool ever deployed."},
        "filters": {"min_liquidity_usd": MIN_LIQUIDITY_USD, "min_volume_24h_usd": MIN_VOLUME_24H_USD, "allowed_quote_or_core_symbols": sorted(ALLOWED_QUOTES)},
        "candidate_count": len(rows),
        "candidates": rows,
        "errors": errors,
        "methodology_warning": "Ranking is discovery only. Fee tier parsing may be unavailable; active liquidity concentration, IL/LVR, token risk and net realized return require a second-stage audit.",
        "source": "GeckoTerminal Public API v2",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(OUTPUT)
    print(json.dumps({"status": result["status"], "candidate_count": len(rows), "errors": errors}))


if __name__ == "__main__":
    asyncio.run(main())
