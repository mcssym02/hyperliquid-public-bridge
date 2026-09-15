from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import health, history, snapshot, xaut

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "market-state-history.json"
KEEP_HOURS = 48
TARGET_HOURS = 1
TARGET_TOLERANCE_MIN = 25
PERPS = ("BTC", "ETH", "SOL", "HYPE")
UNKNOWN = "UNKNOWN"
NA = "NOT_APPLICABLE"


def write_json(name: str, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def as_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def pct_delta(current: Any, previous: Any) -> float | None:
    c, p = as_float(current), as_float(previous)
    if c is None or p is None or p == 0:
        return None
    return (c / p - 1.0) * 100.0


def abs_delta(current: Any, previous: Any) -> float | None:
    c, p = as_float(current), as_float(previous)
    if c is None or p is None:
        return None
    return c - p


def load_history() -> list[dict[str, Any]]:
    if not STATE_FILE.exists():
        return []
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        rows = raw.get("records", []) if isinstance(raw, dict) else []
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []


def compact_record(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    assets: dict[str, Any] = {}
    for row in snapshot_data.get("assets", []):
        if not isinstance(row, dict):
            continue
        asset = row.get("asset")
        if asset not in (*PERPS, "XAUT0"):
            continue
        assets[str(asset)] = {
            "instrument": row.get("instrument", UNKNOWN),
            "type": row.get("type", UNKNOWN),
            "mark_px": row.get("mark_px", UNKNOWN),
            "mid_px": row.get("mid_px", UNKNOWN),
            "open_interest_base": row.get("open_interest_base", NA if asset == "XAUT0" else UNKNOWN),
            "funding_rate_hourly": row.get("funding_rate_hourly", NA if asset == "XAUT0" else UNKNOWN),
            "spot_compare_status": row.get("spot_compare_status", NA if asset == "XAUT0" else "UNAVAILABLE"),
            "spot_compare_instrument": row.get("spot_compare_instrument", NA if asset == "XAUT0" else UNKNOWN),
            "spot_compare_mid_px": row.get("spot_compare_mid_px", NA if asset == "XAUT0" else UNKNOWN),
            "spot_perp_basis_bps": row.get("spot_perp_basis_bps", NA if asset == "XAUT0" else UNKNOWN),
        }
    return {"timestamp_utc": snapshot_data.get("timestamp_utc"), "assets": assets}


def nearest_baseline(records: list[dict[str, Any]], current_ts: float) -> dict[str, Any] | None:
    target = current_ts - TARGET_HOURS * 3600
    tolerance = TARGET_TOLERANCE_MIN * 60
    candidates: list[tuple[float, dict[str, Any]]] = []
    for r in records:
        ts = parse_ts(r.get("timestamp_utc"))
        if ts is None or ts >= current_ts:
            continue
        gap = abs(ts - target)
        if gap <= tolerance:
            candidates.append((gap, r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def classify_spot_perp(cur: dict[str, Any], prev: dict[str, Any] | None) -> str:
    if cur.get("spot_compare_status") != "OK":
        return "INSUFFICIENT_DATA"
    if not prev or prev.get("spot_compare_status") != "OK":
        return "CURRENT_BASIS_ONLY"
    perp_ret = pct_delta(cur.get("mid_px"), prev.get("mid_px"))
    spot_ret = pct_delta(cur.get("spot_compare_mid_px"), prev.get("spot_compare_mid_px"))
    if perp_ret is None or spot_ret is None:
        return "INSUFFICIENT_DATA"
    diff = perp_ret - spot_ret
    if perp_ret * spot_ret < 0:
        return "DIVERGENCE"
    if abs(diff) < 0.05:
        return "CONVERGENT"
    return "PERP_LED" if abs(perp_ret) > abs(spot_ret) else "SPOT_LED"


def derive(snapshot_data: dict[str, Any], records_before_append: list[dict[str, Any]]) -> dict[str, Any]:
    ts_str = snapshot_data.get("timestamp_utc")
    current_ts = parse_ts(ts_str) or datetime.now(timezone.utc).timestamp()
    current = compact_record(snapshot_data)
    baseline = nearest_baseline(records_before_append, current_ts)
    baseline_ts = baseline.get("timestamp_utc") if baseline else None
    baseline_assets = baseline.get("assets", {}) if baseline else {}
    out_assets: list[dict[str, Any]] = []

    for asset in (*PERPS, "XAUT0"):
        cur = current["assets"].get(asset, {})
        prev = baseline_assets.get(asset, {}) if isinstance(baseline_assets, dict) else {}
        if asset == "XAUT0":
            out_assets.append({
                "asset": asset,
                "instrument": cur.get("instrument", "XAUT0/USDC"),
                "type": "spot",
                "current_mid_px": cur.get("mid_px", UNKNOWN),
                "price_change_h1_pct": pct_delta(cur.get("mid_px"), prev.get("mid_px")) if baseline else None,
                "open_interest": NA,
                "delta_oi_h1_abs": NA,
                "delta_oi_h1_pct": NA,
                "funding": NA,
                "funding_change_h1": NA,
                "spot_perp_state": NA,
                "baseline_status": "OK" if baseline else "NOT_RECORDED",
            })
            continue

        out_assets.append({
            "asset": asset,
            "instrument": cur.get("instrument", asset),
            "type": "perp",
            "current_mid_px": cur.get("mid_px", UNKNOWN),
            "price_change_h1_pct": pct_delta(cur.get("mid_px"), prev.get("mid_px")) if baseline else None,
            "open_interest_base": cur.get("open_interest_base", UNKNOWN),
            "delta_oi_h1_abs": abs_delta(cur.get("open_interest_base"), prev.get("open_interest_base")) if baseline else None,
            "delta_oi_h1_pct": pct_delta(cur.get("open_interest_base"), prev.get("open_interest_base")) if baseline else None,
            "funding_rate_hourly": cur.get("funding_rate_hourly", UNKNOWN),
            "funding_change_h1_abs": abs_delta(cur.get("funding_rate_hourly"), prev.get("funding_rate_hourly")) if baseline else None,
            "spot_compare_status": cur.get("spot_compare_status", "UNAVAILABLE"),
            "spot_compare_instrument": cur.get("spot_compare_instrument", UNKNOWN),
            "spot_compare_mid_px": cur.get("spot_compare_mid_px", UNKNOWN),
            "spot_perp_basis_bps": cur.get("spot_perp_basis_bps", UNKNOWN),
            "basis_change_h1_bps": abs_delta(cur.get("spot_perp_basis_bps"), prev.get("spot_perp_basis_bps")) if baseline else None,
            "spot_perp_state": classify_spot_perp(cur, prev if baseline else None),
            "baseline_status": "OK" if baseline else "NOT_RECORDED",
        })

    return {
        "service": "hyperliquid-public-bridge",
        "version": "1.1.0",
        "timestamp_utc": ts_str,
        "status": "OK",
        "baseline_h1_timestamp_utc": baseline_ts,
        "baseline_h1_status": "OK" if baseline else "NOT_RECORDED",
        "baseline_target_minutes": 60,
        "baseline_tolerance_minutes": TARGET_TOLERANCE_MIN,
        "assets": out_assets,
        "liquidations_h1": {
            "status": "UNAVAILABLE",
            "reason": "No reliable public aggregate is produced by this bridge; do not infer liquidations from price/OI.",
        },
    }


def prune_and_append(records: list[dict[str, Any]], new_record: dict[str, Any]) -> list[dict[str, Any]]:
    now_ts = parse_ts(new_record.get("timestamp_utc")) or datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - KEEP_HOURS * 3600
    kept = []
    for r in records:
        ts = parse_ts(r.get("timestamp_utc"))
        if ts is not None and ts >= cutoff:
            kept.append(r)
    kept.append(new_record)
    kept.sort(key=lambda x: parse_ts(x.get("timestamp_utc")) or 0)
    return kept


async def main() -> None:
    health_data, snapshot_data, history_data, xaut_data = await asyncio.gather(
        health(), snapshot(), history(2), xaut()
    )

    previous_records = load_history()
    derived_data = derive(snapshot_data, previous_records)
    updated_records = prune_and_append(previous_records, compact_record(snapshot_data))
    state_data = {
        "service": "hyperliquid-public-bridge",
        "version": "1.1.0",
        "timestamp_utc": snapshot_data.get("timestamp_utc"),
        "keep_hours": KEEP_HOURS,
        "record_count": len(updated_records),
        "records": updated_records,
    }

    write_json("health.json", health_data)
    write_json("snapshot.json", snapshot_data)
    write_json("history-2h.json", history_data)
    write_json("xaut.json", xaut_data)
    write_json("derived.json", derived_data)
    write_json("market-state-history.json", state_data)

    print(json.dumps({
        "health": health_data.get("status"),
        "snapshot": snapshot_data.get("status"),
        "history_2h": history_data.get("status"),
        "xaut": xaut_data.get("status"),
        "derived": derived_data.get("status"),
        "baseline_h1": derived_data.get("baseline_h1_status"),
        "state_records": len(updated_records),
        "timestamp_utc": health_data.get("timestamp_utc"),
    }))


if __name__ == "__main__":
    asyncio.run(main())
