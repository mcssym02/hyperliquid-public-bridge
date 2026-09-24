from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "uniswap-lp.json"
HISTORY_FILE = DATA_DIR / "uniswap-lp-history.json"
MAX_DAYS = 730


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    position = snapshot.get("position", {})
    pool = snapshot.get("pool_state", {})
    principal = snapshot.get("principal", {})
    fees = snapshot.get("fees_collectable", {})
    return {
        "timestamp_utc": snapshot.get("timestamp_utc"),
        "status": snapshot.get("status"),
        "nft_id": position.get("nft_id"),
        "owner_matches_expected": position.get("owner_matches_expected"),
        "pool": snapshot.get("contracts", {}).get("pool"),
        "fee_tier_pct": position.get("fee_tier_pct"),
        "tick_lower": position.get("tick_lower"),
        "tick_upper": position.get("tick_upper"),
        "liquidity": position.get("liquidity"),
        "range_status": pool.get("range_status"),
        "price_usdc_per_weth": pool.get("price_token1_per_token0"),
        "range_lower": pool.get("range_lower_token1_per_token0"),
        "range_upper": pool.get("range_upper_token1_per_token0"),
        "principal_weth": principal.get("token0"),
        "principal_usdc": principal.get("token1"),
        "principal_value_usdc": principal.get("estimated_value_usdc"),
        "fees_weth": fees.get("token0"),
        "fees_usdc": fees.get("token1"),
        "fees_value_usdc": fees.get("estimated_value_usdc"),
        "total_value_usdc": snapshot.get("estimated_total_value_usdc"),
    }


def fee_delta(opening: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    keys = ("fees_weth", "fees_usdc", "fees_value_usdc")
    if any(not isinstance(opening.get(k), (int, float)) or not isinstance(latest.get(k), (int, float)) for k in keys):
        return {"status": "UNKNOWN", "reason": "fee field missing"}
    delta = {k: latest[k] - opening[k] for k in keys}
    reset = delta["fees_weth"] < 0 or delta["fees_usdc"] < 0
    return {
        "status": "RESET_OR_COLLECTION_DETECTED" if reset else "OK",
        "weth": delta["fees_weth"],
        "usdc": delta["fees_usdc"],
        "estimated_value_usdc": delta["fees_value_usdc"],
        "note": "Negative token delta requires collection/rerange event reconstruction." if reset else None,
    }


def main() -> None:
    snapshot = read_json(SNAPSHOT_FILE, {})
    if snapshot.get("status") not in {"OK", "PARTIAL"}:
        raise SystemExit("LP snapshot unavailable; history not modified")
    compact = compact_snapshot(snapshot)
    timestamp = compact.get("timestamp_utc")
    if not isinstance(timestamp, str):
        raise SystemExit("LP snapshot timestamp missing")
    day = timestamp[:10]

    history = read_json(HISTORY_FILE, {"service": "uniswap-v3-lp-daily-history", "version": "1.0.0", "records": []})
    records = history.get("records") if isinstance(history.get("records"), list) else []
    existing = next((r for r in records if r.get("date_utc") == day), None)
    if existing is None:
        records.append({"date_utc": day, "opening": compact, "latest": compact, "fee_delta_today": fee_delta(compact, compact)})
    else:
        existing["latest"] = compact
        existing["fee_delta_today"] = fee_delta(existing.get("opening", {}), compact)

    records.sort(key=lambda r: r.get("date_utc", ""))
    records = records[-MAX_DAYS:]
    history.update({
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "record_count": len(records),
        "records": records,
        "method": "One UTC record per day with immutable opening and refreshed latest snapshot.",
        "limitations": [
            "A fee decrease detects a probable collection or rerange but event logs are needed for exact attribution.",
            "USD fee deltas include WETH price movement; token deltas remain the primary accrual measure.",
        ],
    })
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(HISTORY_FILE)
    print(json.dumps({"status": "OK", "date_utc": day, "record_count": len(records)}))


if __name__ == "__main__":
    main()
