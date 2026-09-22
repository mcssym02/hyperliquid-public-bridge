from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app import health, history, snapshot, xaut

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "market-state-history.json"
LIQ_LAST_GOOD_FILE = DATA_DIR / "liquidations-last-good.json"
KEEP_HOURS = 48
TARGET_HOURS = 1
TARGET_TOLERANCE_MIN = 25
PERPS = ("BTC", "ETH", "SOL", "HYPE")
UNKNOWN = "UNKNOWN"
NA = "NOT_APPLICABLE"

MARGINPAD_BASE = "https://marginpad.io/api/v1"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LIQ_WINDOW_SECONDS = 3600
LIQ_RECENT_MINUTES = 60
LIQ_LIVE_LIMIT = 1000
LIQ_RETRIES = 2
LIQ_CONCURRENCY = 3
LIQ_TIMEOUT = httpx.Timeout(connect=4.0, read=12.0, write=4.0, pool=8.0)
HL_TIMEOUT = httpx.Timeout(connect=4.0, read=12.0, write=4.0, pool=8.0)
TF_CONFIG = {
    "15m": 6 * 3600,
    "1h": 3 * 24 * 3600,
    "4h": 14 * 24 * 3600,
    "1d": 60 * 24 * 3600,
}


def write_json(name: str, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if x > 1e14:
            return x / 1_000_000.0
        if x > 1e11:
            return x / 1000.0
        return x
    text = str(value).strip()
    try:
        return parse_ts(float(text))
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


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


def canonical_symbol(value: Any) -> str:
    s = str(value or "").upper().strip()
    if ":" in s:
        s = s.split(":")[-1]
    for suffix in ("USDT", "USDC", "USD-PERP", "USD", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace("/", "").replace("-", "")


def normalize_exchange(value: Any) -> str:
    s = str(value or "").strip().lower().replace(" ", "_")
    if "hyperliquid" in s or s in {"hl", "hyper_liquid"}:
        return "hyperliquid"
    return s or "unknown"


def normalize_side(value: Any) -> str:
    s = str(value or "").strip().lower()
    if "long" in s:
        return "long"
    if "short" in s:
        return "short"
    if s == "sell":
        return "long"
    if s == "buy":
        return "short"
    return "unknown"


def find_event_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("events", "liquidations", "items", "rows", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            nested = find_event_list(val)
            if nested:
                return nested
    return []


def normalize_liq_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    ts = None
    for k in ("ts", "timestamp", "time", "event_time", "eventTime", "created_at"):
        ts = parse_ts(raw.get(k))
        if ts is not None:
            break
    if ts is None:
        return None

    symbol = canonical_symbol(raw.get("symbol") or raw.get("coin") or raw.get("asset"))
    if not symbol:
        return None
    exchange = normalize_exchange(raw.get("exchange") or raw.get("venue") or raw.get("source"))
    side = normalize_side(raw.get("side") or raw.get("position_side") or raw.get("direction"))
    price = as_float(raw.get("price") or raw.get("px") or raw.get("fill_price"))
    qty = as_float(raw.get("qty") or raw.get("quantity") or raw.get("size") or raw.get("sz"))
    notional = as_float(raw.get("notional") or raw.get("value_usd") or raw.get("notional_usd") or raw.get("value"))
    if notional is None and price is not None and qty is not None:
        notional = abs(price * qty)
    if notional is None:
        return None

    return {
        "ts_ms": int(ts * 1000),
        "timestamp_utc": iso_utc(ts),
        "exchange": exchange,
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "notional_usd": abs(notional),
    }


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        key = (
            e.get("ts_ms"), e.get("exchange"), e.get("symbol"), e.get("side"),
            e.get("price"), e.get("qty"), e.get("notional_usd"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda x: int(x.get("ts_ms", 0)))
    return out


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    long_events = [e for e in events if e.get("side") == "long"]
    short_events = [e for e in events if e.get("side") == "short"]
    unknown_events = [e for e in events if e.get("side") not in {"long", "short"}]
    by_exchange: dict[str, dict[str, Any]] = {}
    for e in events:
        ex = str(e.get("exchange") or "unknown")
        row = by_exchange.setdefault(ex, {
            "count": 0, "total_usd": 0.0,
            "long_count": 0, "long_usd": 0.0,
            "short_count": 0, "short_usd": 0.0,
            "unknown_side_count": 0, "unknown_side_usd": 0.0,
        })
        n = float(e.get("notional_usd") or 0.0)
        row["count"] += 1
        row["total_usd"] += n
        side = e.get("side")
        if side == "long":
            row["long_count"] += 1
            row["long_usd"] += n
        elif side == "short":
            row["short_count"] += 1
            row["short_usd"] += n
        else:
            row["unknown_side_count"] += 1
            row["unknown_side_usd"] += n
    return {
        "count": len(events),
        "total_usd": sum(float(e.get("notional_usd") or 0.0) for e in events),
        "long_count": len(long_events),
        "long_usd": sum(float(e.get("notional_usd") or 0.0) for e in long_events),
        "short_count": len(short_events),
        "short_usd": sum(float(e.get("notional_usd") or 0.0) for e in short_events),
        "unknown_side_count": len(unknown_events),
        "unknown_side_usd": sum(float(e.get("notional_usd") or 0.0) for e in unknown_events),
        "by_exchange": by_exchange,
    }


def norm_key(value: Any) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def pick_numeric(row: dict[str, Any], side: str) -> float | None:
    aliases = {
        "long": {
            "long", "longs", "longusd", "longvalue", "longnotional", "longamount",
            "longliquidated", "longliquidation", "longliquidations", "longliq", "longliqus",
        },
        "short": {
            "short", "shorts", "shortusd", "shortvalue", "shortnotional", "shortamount",
            "shortliquidated", "shortliquidation", "shortliquidations", "shortliq", "shortliqus",
        },
    }
    normalized = {norm_key(k): v for k, v in row.items()}
    for key in aliases[side]:
        v = as_float(normalized.get(key))
        if v is not None:
            return abs(v)
    return None


def find_histogram_rows(payload: Any) -> list[dict[str, Any]]:
    candidates: list[list[dict[str, Any]]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts:
                scored = sum(1 for r in dicts if pick_numeric(r, "long") is not None or pick_numeric(r, "short") is not None)
                if scored:
                    candidates.append(dicts)
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda rows: sum(1 for r in rows if pick_numeric(r, "long") is not None or pick_numeric(r, "short") is not None), reverse=True)
    return candidates[0]


def summarize_histogram(payload: Any) -> dict[str, Any]:
    rows = find_histogram_rows(payload)
    if not rows:
        return {
            "status": "PARSE_ERROR",
            "bucket_count": 0,
            "long_usd": None,
            "short_usd": None,
            "total_usd": None,
            "note": "No time-bucket list with recognizable long/short fields was found.",
        }

    long_total = 0.0
    short_total = 0.0
    long_found = False
    short_found = False
    timestamps: list[float] = []
    used = 0

    for row in rows:
        lv = pick_numeric(row, "long")
        sv = pick_numeric(row, "short")
        if lv is None and sv is None:
            continue
        used += 1
        if lv is not None:
            long_total += lv
            long_found = True
        if sv is not None:
            short_total += sv
            short_found = True
        for k in ("ts", "timestamp", "time", "bucket", "bucket_ts", "bucketTime", "start"):
            t = parse_ts(row.get(k))
            if t is not None:
                timestamps.append(t)
                break

    if not long_found and not short_found:
        return {
            "status": "PARSE_ERROR",
            "bucket_count": 0,
            "long_usd": None,
            "short_usd": None,
            "total_usd": None,
            "note": "Histogram rows were found but no numeric long/short values could be parsed.",
        }

    return {
        "status": "OK",
        "bucket_count": used,
        "long_usd": long_total,
        "short_usd": short_total,
        "total_usd": long_total + short_total,
        "earliest_bucket_utc": iso_utc(min(timestamps)) if timestamps else None,
        "latest_bucket_utc": iso_utc(max(timestamps)) if timestamps else None,
        "note": "Summed from MarginPad time-bucketed observed liquidation archive for the requested 60-minute window.",
    }


async def fetch_marginpad(
    client: httpx.AsyncClient,
    path: str,
    semaphore: asyncio.Semaphore,
) -> tuple[Any | None, str | None]:
    last_error: str | None = None
    for attempt in range(1, LIQ_RETRIES + 1):
        try:
            async with semaphore:
                r = await client.get(f"{MARGINPAD_BASE}{path}")
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, dict) and payload.get("ok") is False:
                return None, f"API error: {payload.get('error')}"
            return payload, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
            if attempt < LIQ_RETRIES:
                await asyncio.sleep(0.8 * attempt)
    return None, last_error or "unknown MarginPad error"


async def collect_liquidations() -> dict[str, Any]:
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - LIQ_WINDOW_SECONDS
    headers = {"User-Agent": "hyperliquid-public-bridge/1.6 liquidation-audit"}
    semaphore = asyncio.Semaphore(LIQ_CONCURRENCY)
    async with httpx.AsyncClient(timeout=LIQ_TIMEOUT, headers=headers, follow_redirects=True) as client:
        recent_tasks = {
            a: asyncio.create_task(fetch_marginpad(client, f"/liquidations/recent?symbol={a}&minutes={LIQ_RECENT_MINUTES}", semaphore))
            for a in PERPS
        }
        live_tasks = {
            a: asyncio.create_task(fetch_marginpad(client, f"/liquidations/live?symbol={a}&limit={LIQ_LIVE_LIMIT}", semaphore))
            for a in PERPS
        }
        feed_task = asyncio.create_task(fetch_marginpad(client, "/feed", semaphore))
        feed_payload, feed_error = await feed_task
        recent_results = {a: await task for a, task in recent_tasks.items()}
        live_results = {a: await task for a, task in live_tasks.items()}

    feed_events: list[dict[str, Any]] = []
    if feed_payload is not None:
        feed_events = [e for x in find_event_list(feed_payload) if (e := normalize_liq_event(x)) is not None]

    assets: list[dict[str, Any]] = []
    source_errors: dict[str, Any] = {"feed": feed_error, "recent": {}, "live": {}}
    any_ok = feed_payload is not None

    for asset in PERPS:
        recent_payload, recent_err = recent_results[asset]
        live_payload, live_err = live_results[asset]
        source_errors["recent"][asset] = recent_err
        source_errors["live"][asset] = live_err
        if recent_payload is not None or live_payload is not None:
            any_ok = True

        global_h1 = summarize_histogram(recent_payload) if recent_payload is not None else {
            "status": "UNAVAILABLE",
            "bucket_count": 0,
            "long_usd": None,
            "short_usd": None,
            "total_usd": None,
            "note": recent_err or "60-minute archive endpoint unavailable.",
        }

        live_events: list[dict[str, Any]] = []
        if live_payload is not None:
            live_events = [e for x in find_event_list(live_payload) if (e := normalize_liq_event(x)) is not None]
        combined = dedupe_events([e for e in live_events + feed_events if e.get("symbol") == asset])
        window_events = [e for e in combined if (e.get("ts_ms", 0) / 1000.0) >= cutoff]
        hl_events = [e for e in window_events if e.get("exchange") == "hyperliquid"]

        earliest_all = min((e.get("ts_ms", 0) for e in combined), default=0) / 1000.0 if combined else None
        latest_all = max((e.get("ts_ms", 0) for e in combined), default=0) / 1000.0 if combined else None
        live_ok = live_payload is not None
        if live_ok and earliest_all is not None and earliest_all <= cutoff:
            hl_coverage = "FULL_WINDOW_FROM_RAW_EVENTS"
        elif live_ok and combined:
            hl_coverage = "PARTIAL_WINDOW_RAW_EVENTS_DO_NOT_REACH_H-1"
        elif feed_payload is not None and combined:
            hl_coverage = "PARTIAL_FEED_ONLY"
        elif live_ok:
            hl_coverage = "EMPTY_OR_QUIET_WINDOW"
        else:
            hl_coverage = "UNAVAILABLE"

        global_coverage = "FULL_60M_ARCHIVE" if global_h1.get("status") == "OK" else global_h1.get("status", "UNAVAILABLE")

        assets.append({
            "asset": asset,
            "window_minutes": LIQ_RECENT_MINUTES,
            "global_coverage_status": global_coverage,
            "global_h1": global_h1,
            "hyperliquid_coverage_status": hl_coverage,
            "hyperliquid_observed_h1": summarize_events(hl_events),
            "hyperliquid_earliest_raw_event_utc": iso_utc(earliest_all),
            "hyperliquid_latest_raw_event_utc": iso_utc(latest_all),
            "hyperliquid_source": "MarginPad public collector of Hyperliquid forced-liquidation stream; secondary observed source, not Hyperliquid official REST.",
            "raw_event_limit": LIQ_LIVE_LIMIT,
            "raw_event_sample_count_total": len(combined),
            "raw_event_sample_count_h1": len(window_events),
        })

    return {
        "service": "hyperliquid-public-bridge",
        "version": "1.6.0",
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "OK" if any_ok else "UNAVAILABLE",
        "window_minutes": LIQ_RECENT_MINUTES,
        "source": {
            "provider": "MarginPad",
            "authentication_required": False,
            "global_h1_endpoint": "/api/v1/liquidations/recent?symbol={ASSET}&minutes=60",
            "hyperliquid_raw_endpoint": f"/api/v1/liquidations/live?symbol={{ASSET}}&limit={LIQ_LIVE_LIMIT}",
            "methodology": "Global H-1 comes from MarginPad's measured time-bucketed liquidation archive. Hyperliquid-specific H-1 is computed only from returned raw events and carries an explicit coverage status.",
            "documented_venues": ["binance", "bybit", "okx", "bitmex", "hyperliquid", "bitfinex", "gate", "htx", "dydx"],
            "documented_market_coverage": "approximately 70%+ of liquidation flow; observed, not extrapolated",
        },
        "assets": assets,
        "source_errors": source_errors,
    }



def nearest_record_hours(
    records: list[dict[str, Any]],
    current_ts: float,
    hours: int,
) -> dict[str, Any] | None:
    target = current_ts - hours * 3600
    tolerance = (35 * 60) if hours <= 4 else (90 * 60)
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


def derivative_horizons(
    snapshot_data: dict[str, Any],
    records_before_append: list[dict[str, Any]],
    asset: str,
) -> dict[str, Any]:
    current_ts = parse_ts(snapshot_data.get("timestamp_utc")) or datetime.now(timezone.utc).timestamp()
    current = compact_record(snapshot_data).get("assets", {}).get(asset, {})
    out: dict[str, Any] = {}
    for hours in (1, 4, 24):
        baseline = nearest_record_hours(records_before_append, current_ts, hours)
        if not baseline:
            out[f"{hours}h"] = {"status": "NOT_RECORDED"}
            continue
        prev = baseline.get("assets", {}).get(asset, {})
        cur_f = as_float(current.get("funding_rate_hourly"))
        prev_f = as_float(prev.get("funding_rate_hourly"))
        perp_ret = pct_delta(current.get("mid_px"), prev.get("mid_px"))
        spot_ret = pct_delta(current.get("spot_compare_mid_px"), prev.get("spot_compare_mid_px"))
        out[f"{hours}h"] = {
            "status": "OK",
            "baseline_timestamp_utc": baseline.get("timestamp_utc"),
            "price_change_pct": perp_ret,
            "open_interest_change_abs_base": abs_delta(current.get("open_interest_base"), prev.get("open_interest_base")),
            "open_interest_change_pct": pct_delta(current.get("open_interest_base"), prev.get("open_interest_base")),
            "funding_change_decimal": (cur_f - prev_f) if cur_f is not None and prev_f is not None else None,
            "funding_change_pct_points": ((cur_f - prev_f) * 100.0) if cur_f is not None and prev_f is not None else None,
            "basis_change_bps": abs_delta(current.get("spot_perp_basis_bps"), prev.get("spot_perp_basis_bps")),
            "spot_price_change_pct": spot_ret,
            "perp_minus_spot_return_pp": (perp_ret - spot_ret) if perp_ret is not None and spot_ret is not None else None,
        }
    return out


def tf_candle_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_time_ms": raw.get("t"),
        "close_time_ms": raw.get("T"),
        "open": as_float(raw.get("o")),
        "high": as_float(raw.get("h")),
        "low": as_float(raw.get("l")),
        "close": as_float(raw.get("c")),
        "volume_base": as_float(raw.get("v")),
        "trades": int(raw.get("n", 0) or 0),
    }


def summarize_tf_candles(raw: Any, now_ms: int) -> dict[str, Any]:
    if not isinstance(raw, list):
        return {"status": "UNAVAILABLE"}
    rows = [tf_candle_row(x) for x in raw if isinstance(x, dict)]
    rows = [x for x in rows if x.get("open_time_ms") is not None]
    rows.sort(key=lambda x: int(x.get("open_time_ms") or 0))
    closed = [x for x in rows if int(x.get("close_time_ms") or 0) <= now_ms]
    forming = next(
        (x for x in reversed(rows)
         if int(x.get("open_time_ms") or 0) <= now_ms < int(x.get("close_time_ms") or 0)),
        None,
    )
    if len(closed) < 2:
        return {
            "status": "INSUFFICIENT_DATA",
            "closed_candle_count": len(closed),
            "forming_candle": forming,
        }

    recent = closed[-8:]
    structure_rows = closed[-5:]
    highs = [x["high"] for x in recent if x.get("high") is not None]
    lows = [x["low"] for x in recent if x.get("low") is not None]
    vols = [x["volume_base"] for x in recent if x.get("volume_base") is not None]

    hh = hl = lh = ll = 0
    for prev, cur in zip(structure_rows, structure_rows[1:]):
        if prev.get("high") is not None and cur.get("high") is not None:
            hh += int(cur["high"] > prev["high"])
            lh += int(cur["high"] < prev["high"])
        if prev.get("low") is not None and cur.get("low") is not None:
            hl += int(cur["low"] > prev["low"])
            ll += int(cur["low"] < prev["low"])

    if hh >= 3 and hl >= 3:
        hint = "MECHANICAL_UP"
    elif lh >= 3 and ll >= 3:
        hint = "MECHANICAL_DOWN"
    else:
        hint = "MECHANICAL_MIXED"

    first_open = recent[0].get("open")
    last_close = recent[-1].get("close")
    lookback_change = None
    if first_open not in (None, 0) and last_close is not None:
        lookback_change = (last_close / first_open - 1.0) * 100.0

    return {
        "status": "OK",
        "closed_candle_count": len(closed),
        "last_closed": closed[-1],
        "previous_closed": closed[-2],
        "forming_candle": forming,
        "recent_8_high": max(highs) if highs else None,
        "recent_8_low": min(lows) if lows else None,
        "recent_8_volume_base": sum(vols) if vols else None,
        "recent_8_change_pct": lookback_change,
        "mechanical_structure_hint": hint,
        "pairwise_counts_last_5": {
            "higher_high": hh,
            "higher_low": hl,
            "lower_high": lh,
            "lower_low": ll,
        },
        "note": "Mechanical candle summary only; Radar must still interpret regime, acceptance/retest and context.",
    }


async def fetch_hl_candles(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> tuple[Any | None, str | None]:
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    last_error: str | None = None
    for attempt in range(1, 3):
        try:
            async with semaphore:
                r = await client.post(HL_INFO_URL, json=payload)
            r.raise_for_status()
            return r.json(), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
            if attempt < 2:
                await asyncio.sleep(0.5 * attempt)
    return None, last_error or "unknown Hyperliquid candle error"


async def collect_multitf(xaut_data: dict[str, Any]) -> dict[str, Any]:
    now_ts = datetime.now(timezone.utc).timestamp()
    now_ms = int(now_ts * 1000)
    xaut_coin = xaut_data.get("api_coin") if isinstance(xaut_data, dict) else None
    coins = {a: a for a in PERPS}
    if xaut_coin:
        coins["XAUT0"] = str(xaut_coin)

    semaphore = asyncio.Semaphore(5)
    tasks: dict[tuple[str, str], asyncio.Task] = {}
    headers = {"User-Agent": "hyperliquid-public-bridge/1.6 multi-tf"}
    async with httpx.AsyncClient(timeout=HL_TIMEOUT, headers=headers) as client:
        for asset, coin in coins.items():
            for interval, seconds in TF_CONFIG.items():
                start_ms = int((now_ts - seconds) * 1000)
                tasks[(asset, interval)] = asyncio.create_task(
                    fetch_hl_candles(client, semaphore, coin, interval, start_ms, now_ms)
                )
        results = {k: await task for k, task in tasks.items()}

    assets: dict[str, Any] = {}
    any_ok = False
    for asset in (*PERPS, "XAUT0"):
        if asset not in coins:
            assets[asset] = {"status": "UNAVAILABLE", "error": "instrument unresolved"}
            continue
        tfs: dict[str, Any] = {}
        for interval in TF_CONFIG:
            raw, err = results[(asset, interval)]
            summary = summarize_tf_candles(raw, now_ms)
            if err:
                summary["error"] = err
            if summary.get("status") == "OK":
                any_ok = True
            tfs[interval] = summary
        statuses = [x.get("status") for x in tfs.values()]
        asset_status = "OK" if statuses and all(x == "OK" for x in statuses) else "PARTIAL" if any(x == "OK" for x in statuses) else "UNAVAILABLE"
        assets[asset] = {"status": asset_status, "timeframes": tfs}

    return {
        "service": "hyperliquid-public-bridge",
        "version": "1.6.0",
        "timestamp_utc": iso_utc(now_ts),
        "status": "OK" if assets and all(v.get("status") == "OK" for v in assets.values()) else "PARTIAL" if any_ok else "UNAVAILABLE",
        "source": "Hyperliquid official public candleSnapshot",
        "assets": assets,
    }


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
    except Exception:
        return None
    return None


def liquidation_last_good(
    current: dict[str, Any],
) -> dict[str, Any] | None:
    rows = [x for x in current.get("assets", []) if isinstance(x, dict)]
    all_global_full = bool(rows) and all(x.get("global_coverage_status") == "FULL_60M_ARCHIVE" for x in rows)
    if all_global_full:
        write_json("liquidations-last-good.json", current)
        return current
    return load_json_file(LIQ_LAST_GOOD_FILE)


def derive(snapshot_data: dict[str, Any], records_before_append: list[dict[str, Any]], liquidations_data: dict[str, Any]) -> dict[str, Any]:
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

    liq_assets = {r.get("asset"): r for r in liquidations_data.get("assets", []) if isinstance(r, dict)}
    return {
        "service": "hyperliquid-public-bridge",
        "version": "1.6.0",
        "timestamp_utc": ts_str,
        "status": "OK",
        "baseline_h1_timestamp_utc": baseline_ts,
        "baseline_h1_status": "OK" if baseline else "NOT_RECORDED",
        "baseline_target_minutes": 60,
        "baseline_tolerance_minutes": TARGET_TOLERANCE_MIN,
        "assets": out_assets,
        "liquidations_h1": {
            "status": liquidations_data.get("status", "UNAVAILABLE"),
            "source": "MarginPad keyless public realized-liquidation collector",
            "posture": "SECONDARY_OBSERVED",
            "official_hyperliquid_marketwide_endpoint": "NOT_AVAILABLE",
            "assets": {a: liq_assets.get(a, {"global_coverage_status": "UNAVAILABLE", "hyperliquid_coverage_status": "UNAVAILABLE"}) for a in PERPS},
            "rule": "Use global_h1 when global_coverage_status=FULL_60M_ARCHIVE. Read Hyperliquid raw coverage separately; never infer missing liquidations from price/OI.",
        },
    }



def h1_path_summary(history_data: dict[str, Any], asset: str, current_ts: float) -> dict[str, Any]:
    row = next(
        (r for r in history_data.get("assets", []) if isinstance(r, dict) and r.get("asset") == asset),
        None,
    )
    if not row:
        return {"status": "UNAVAILABLE"}
    candles = [x for x in row.get("candles", []) if isinstance(x, dict)]
    cutoff_ms = int((current_ts - 3600) * 1000)
    end_ms = int(current_ts * 1000)
    selected = [
        x for x in candles
        if int(x.get("close_time_ms", 0) or 0) >= cutoff_ms
        and int(x.get("open_time_ms", 0) or 0) <= end_ms
    ]
    if not selected:
        return {"status": "NOT_RECORDED"}

    opens = [as_float(x.get("open")) for x in selected]
    highs = [as_float(x.get("high")) for x in selected]
    lows = [as_float(x.get("low")) for x in selected]
    closes = [as_float(x.get("close")) for x in selected]
    opens = [x for x in opens if x is not None]
    highs = [x for x in highs if x is not None]
    lows = [x for x in lows if x is not None]
    closes = [x for x in closes if x is not None]
    if not opens or not highs or not lows or not closes:
        return {"status": "INSUFFICIENT_DATA"}

    start_open = opens[0]
    end_close = closes[-1]
    hi = max(highs)
    lo = min(lows)
    vol = sum(as_float(x.get("volume_base")) or 0.0 for x in selected)
    trades = sum(int(x.get("trades", 0) or 0) for x in selected)

    def rel(v: float) -> float | None:
        return ((v / start_open) - 1.0) * 100.0 if start_open else None

    return {
        "status": "OK",
        "candle_count": len(selected),
        "start_open": start_open,
        "end_close": end_close,
        "high": hi,
        "low": lo,
        "close_change_pct": rel(end_close),
        "max_up_from_start_pct": rel(hi),
        "max_down_from_start_pct": rel(lo),
        "range_pct_of_start": ((hi - lo) / start_open * 100.0) if start_open else None,
        "volume_base": vol,
        "trades": trades,
        "note": "Last ~60 minutes of 1m candles. Use this for path/excursions; do not confuse with snapshot-to-snapshot price_change_h1_pct.",
    }


def build_radar_core(
    snapshot_data: dict[str, Any],
    derived_data: dict[str, Any],
    liquidations_data: dict[str, Any],
    liquidations_last_good_data: dict[str, Any] | None,
    xaut_data: dict[str, Any],
    history_data: dict[str, Any],
    multi_tf_data: dict[str, Any],
    records_before_append: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compact decision feed: primary market + multi-TF + multi-horizon derivatives."""
    snap_assets = {
        r.get("asset"): r for r in snapshot_data.get("assets", [])
        if isinstance(r, dict)
    }
    der_assets = {
        r.get("asset"): r for r in derived_data.get("assets", [])
        if isinstance(r, dict)
    }
    liq_assets = {
        r.get("asset"): r for r in liquidations_data.get("assets", [])
        if isinstance(r, dict)
    }
    last_good_assets = {
        r.get("asset"): r for r in (liquidations_last_good_data or {}).get("assets", [])
        if isinstance(r, dict)
    }
    mtf_assets = multi_tf_data.get("assets", {}) if isinstance(multi_tf_data.get("assets"), dict) else {}
    current_ts = parse_ts(snapshot_data.get("timestamp_utc")) or datetime.now(timezone.utc).timestamp()
    last_good_ts = parse_ts((liquidations_last_good_data or {}).get("timestamp_utc"))
    last_good_age_minutes = ((current_ts - last_good_ts) / 60.0) if last_good_ts is not None else None

    assets: dict[str, Any] = {}
    for asset in PERPS:
        s = snap_assets.get(asset, {})
        d = der_assets.get(asset, {})
        l = liq_assets.get(asset, {})
        lg = last_good_assets.get(asset, {})
        g = l.get("global_h1", {}) if isinstance(l.get("global_h1"), dict) else {}
        h = l.get("hyperliquid_observed_h1", {}) if isinstance(l.get("hyperliquid_observed_h1"), dict) else {}
        mid = as_float(s.get("mid_px") or d.get("current_mid_px"))
        oi_base = as_float(s.get("open_interest_base") or d.get("open_interest_base"))
        doi_base = as_float(d.get("delta_oi_h1_abs"))
        funding_decimal = as_float(s.get("funding_rate_hourly") or d.get("funding_rate_hourly"))
        funding_delta_decimal = as_float(d.get("funding_change_h1_abs"))

        last_good_liq = None
        if l.get("global_coverage_status") != "FULL_60M_ARCHIVE" and lg:
            lgg = lg.get("global_h1", {}) if isinstance(lg.get("global_h1"), dict) else {}
            lgh = lg.get("hyperliquid_observed_h1", {}) if isinstance(lg.get("hyperliquid_observed_h1"), dict) else {}
            last_good_liq = {
                "timestamp_utc": (liquidations_last_good_data or {}).get("timestamp_utc"),
                "age_minutes": last_good_age_minutes,
                "historical_only": True,
                "global_coverage_status": lg.get("global_coverage_status"),
                "global_h1": lgg,
                "hyperliquid_coverage_status": lg.get("hyperliquid_coverage_status"),
                "hyperliquid_observed_h1": lgh,
                "rule": "Historical fallback only; never substitute for current H-1 liquidation flow.",
            }

        assets[asset] = {
            "instrument": s.get("instrument", asset),
            "type": "perp",
            "status": s.get("status", UNKNOWN),
            "mid_px": s.get("mid_px", d.get("current_mid_px", UNKNOWN)),
            "mark_px": s.get("mark_px", UNKNOWN),
            "high_24h": s.get("high_24h", UNKNOWN),
            "low_24h": s.get("low_24h", UNKNOWN),
            "best_bid": s.get("best_bid", UNKNOWN),
            "best_ask": s.get("best_ask", UNKNOWN),
            "volume_24h_notional_usd": s.get("volume_24h_notional", UNKNOWN),

            "price_change_h1_pct": d.get("price_change_h1_pct"),
            "price_change_h1_definition": "snapshot midpoint now vs baseline midpoint ~60m ago; NOT the full intrahour path",
            "h1_path": h1_path_summary(history_data, asset, current_ts),
            "multi_tf": mtf_assets.get(asset, {"status": "UNAVAILABLE"}),
            "derivatives_horizons": derivative_horizons(snapshot_data, records_before_append, asset),

            "open_interest_base": s.get("open_interest_base", d.get("open_interest_base", UNKNOWN)),
            "open_interest_unit": asset,
            "open_interest_notional_usd_est": (oi_base * mid) if oi_base is not None and mid is not None else None,
            "delta_oi_h1_abs_base": d.get("delta_oi_h1_abs"),
            "delta_oi_h1_unit": asset,
            "delta_oi_h1_pct": d.get("delta_oi_h1_pct"),
            "delta_oi_h1_notional_usd_at_current_mid_est": (doi_base * mid) if doi_base is not None and mid is not None else None,

            "funding_rate_hourly_decimal": s.get("funding_rate_hourly", d.get("funding_rate_hourly", UNKNOWN)),
            "funding_rate_hourly_pct": (funding_decimal * 100.0) if funding_decimal is not None else None,
            "funding_change_h1_decimal": d.get("funding_change_h1_abs"),
            "funding_change_h1_pct_points": (funding_delta_decimal * 100.0) if funding_delta_decimal is not None else None,
            "funding_interval": s.get("funding_interval", "1h"),

            "spot_compare_status": d.get("spot_compare_status", s.get("spot_compare_status", "UNAVAILABLE")),
            "spot_compare_instrument": d.get("spot_compare_instrument", s.get("spot_compare_instrument", UNKNOWN)),
            "spot_compare_mid_px": d.get("spot_compare_mid_px", s.get("spot_compare_mid_px", UNKNOWN)),
            "spot_perp_basis_bps": d.get("spot_perp_basis_bps", s.get("spot_perp_basis_bps", UNKNOWN)),
            "basis_change_h1_bps": d.get("basis_change_h1_bps"),
            "spot_perp_state": d.get("spot_perp_state", "INSUFFICIENT_DATA"),
            "baseline_status": d.get("baseline_status", derived_data.get("baseline_h1_status", "NOT_RECORDED")),

            "liquidations_global_h1": {
                "coverage_status": l.get("global_coverage_status", "UNAVAILABLE"),
                "status": g.get("status", "UNAVAILABLE"),
                "bucket_count": g.get("bucket_count"),
                "bucket_count_semantics": "number of returned archive buckets; NOT minutes of coverage",
                "long_positions_liquidated_usd": g.get("long_usd"),
                "short_positions_liquidated_usd": g.get("short_usd"),
                "total_usd": g.get("total_usd"),
                "scope": "secondary observed multi-venue archive; approximately 70%+ documented market coverage, not exhaustive",
            },
            "liquidations_hyperliquid_h1": {
                "coverage_status": l.get("hyperliquid_coverage_status", "UNAVAILABLE"),
                "count": h.get("count"),
                "long_positions_liquidated_usd": h.get("long_usd"),
                "short_positions_liquidated_usd": h.get("short_usd"),
                "total_usd": h.get("total_usd"),
                "raw_event_limit": l.get("raw_event_limit"),
                "earliest_raw_event_utc": l.get("hyperliquid_earliest_raw_event_utc"),
                "latest_raw_event_utc": l.get("hyperliquid_latest_raw_event_utc"),
                "scope": "secondary observed Hyperliquid event stream, not official market-wide REST",
            },
            "liquidations_last_good": last_good_liq,
        }

    sx = snap_assets.get("XAUT0", {})
    dx = der_assets.get("XAUT0", {})
    assets["XAUT0"] = {
        "instrument": sx.get("instrument", xaut_data.get("instrument", "XAUT0/USDC")),
        "type": "spot",
        "status": sx.get("status", xaut_data.get("status", UNKNOWN)),
        "mid_px": sx.get("mid_px", dx.get("current_mid_px", UNKNOWN)),
        "mark_px": sx.get("mark_px", xaut_data.get("mark_px", UNKNOWN)),
        "high_24h": sx.get("high_24h", UNKNOWN),
        "low_24h": sx.get("low_24h", UNKNOWN),
        "best_bid": sx.get("best_bid", UNKNOWN),
        "best_ask": sx.get("best_ask", UNKNOWN),
        "volume_24h_notional_usd": sx.get("volume_24h_notional", UNKNOWN),
        "price_change_h1_pct": dx.get("price_change_h1_pct"),
        "price_change_h1_definition": "snapshot midpoint now vs baseline midpoint ~60m ago; NOT the full intrahour path",
        "h1_path": h1_path_summary(history_data, "XAUT0", current_ts),
        "multi_tf": mtf_assets.get("XAUT0", {"status": "UNAVAILABLE"}),
        "derivatives_perp": NA,
        "baseline_status": dx.get("baseline_status", derived_data.get("baseline_h1_status", "NOT_RECORDED")),
        "identity_guard": "Exact Hyperliquid XAUT0/USDC spot; never substitute xyz:GOLD/XAU/GC levels.",
        "weekend_guard": "XAUT0 may trade while traditional gold venues are closed; do not treat weekend XAUT0 moves as confirmed XAU/GC moves.",
    }

    primary_market_status = snapshot_data.get("status", UNKNOWN)
    mtf_status = multi_tf_data.get("status", "UNAVAILABLE")
    h1_status = derived_data.get("baseline_h1_status", "NOT_RECORDED")
    liq_status = liquidations_data.get("status", "UNAVAILABLE")
    if primary_market_status == "OK" and h1_status == "OK" and mtf_status in ("OK", "PARTIAL"):
        decision_status = "OK" if liq_status == "OK" else "DEGRADED_SECONDARY"
    else:
        decision_status = "DEGRADED_PRIMARY"

    return {
        "service": "hyperliquid-public-bridge",
        "version": "1.6.0",
        "timestamp_utc": snapshot_data.get("timestamp_utc"),
        "status": decision_status,
        "component_status": {
            "primary_market": primary_market_status,
            "h1_baseline": h1_status,
            "multi_tf": mtf_status,
            "liquidations_current": liq_status,
            "liquidations_last_good_timestamp_utc": (liquidations_last_good_data or {}).get("timestamp_utc"),
            "liquidations_last_good_age_minutes": last_good_age_minutes,
            "xaut_exact": xaut_data.get("status", "UNAVAILABLE"),
        },
        "data_quality_rule": "A secondary liquidation outage must not mark primary market/OI/funding/multi-TF data unavailable. Report the missing component explicitly.",
        "baseline_h1_timestamp_utc": derived_data.get("baseline_h1_timestamp_utc"),
        "baseline_h1_status": h1_status,
        "liquidations_status": liq_status,
        "source_policy": {
            "market": "Hyperliquid official public API",
            "multi_tf": "Hyperliquid official public candleSnapshot",
            "liquidations": "MarginPad secondary observed; global archive + venue-specific raw coverage",
            "auth_required": False,
        },
        "unit_schema": {
            "price_change_h1_pct": "percent",
            "open_interest_base": "base asset units, NOT USD",
            "delta_oi_h1_abs_base": "base asset units, NOT USD",
            "open_interest_notional_usd_est": "estimated USD at current midpoint",
            "funding_rate_hourly_decimal": "decimal rate, e.g. 0.0000125 = 0.00125%",
            "funding_rate_hourly_pct": "percent per funding interval",
            "spot_perp_basis_bps": "basis points",
            "liquidations": "USD notional of positions liquidated",
        },
        "interpretation_rules": [
            "Long positions liquidated = forced sell-side flow; short positions liquidated = forced buy-to-cover flow.",
            "Price up/down plus OI up/down does not identify the side of new positioning by itself.",
            "Positive funding means longs pay shorts; it is not automatically crowded-long evidence unless magnitude is elevated in context.",
            "Negative funding means shorts pay longs; it is not automatically crowded-short evidence unless magnitude is elevated in context.",
            "Basis sign alone is not a directional trigger; use magnitude, change, spot/perp state and structure.",
            "price_change_h1_pct is endpoint-to-endpoint; use h1_path for intrahour sweeps, breakouts and reversals.",
            "multi_tf mechanical hints are inputs, not trade signals; acceptance/retest and regime interpretation remain required.",
            "Global liquidation archive and Hyperliquid OI are different venue scopes; do not normalize one mechanically by the other.",
            "Last-good liquidations are historical context only and never replace current H-1 liquidation flow.",
        ],
        "assets": assets,
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
    health_data, snapshot_data, history_data, xaut_data, liquidations_data = await asyncio.gather(
        health(), snapshot(), history(2), xaut(), collect_liquidations()
    )
    multi_tf_data = await collect_multitf(xaut_data)
    liquidations_last_good_data = liquidation_last_good(liquidations_data)

    previous_records = load_history()
    derived_data = derive(snapshot_data, previous_records, liquidations_data)
    updated_records = prune_and_append(previous_records, compact_record(snapshot_data))
    state_data = {
        "service": "hyperliquid-public-bridge",
        "version": "1.6.0",
        "timestamp_utc": snapshot_data.get("timestamp_utc"),
        "keep_hours": KEEP_HOURS,
        "record_count": len(updated_records),
        "records": updated_records,
    }
    radar_core_data = build_radar_core(
        snapshot_data,
        derived_data,
        liquidations_data,
        liquidations_last_good_data,
        xaut_data,
        history_data,
        multi_tf_data,
        previous_records,
    )

    write_json("health.json", health_data)
    write_json("snapshot.json", snapshot_data)
    write_json("history-2h.json", history_data)
    write_json("xaut.json", xaut_data)
    write_json("liquidations.json", liquidations_data)
    write_json("derived.json", derived_data)
    write_json("multi-tf.json", multi_tf_data)
    write_json("radar-core.json", radar_core_data)
    write_json("market-state-history.json", state_data)

    print(json.dumps({
        "health": health_data.get("status"),
        "snapshot": snapshot_data.get("status"),
        "history_2h": history_data.get("status"),
        "xaut": xaut_data.get("status"),
        "liquidations": liquidations_data.get("status"),
        "liq_global_coverage": {a.get("asset"): a.get("global_coverage_status") for a in liquidations_data.get("assets", [])},
        "liq_hl_coverage": {a.get("asset"): a.get("hyperliquid_coverage_status") for a in liquidations_data.get("assets", [])},
        "derived": derived_data.get("status"),
        "multi_tf": multi_tf_data.get("status"),
        "radar_core": radar_core_data.get("status"),
        "baseline_h1": derived_data.get("baseline_h1_status"),
        "state_records": len(updated_records),
        "timestamp_utc": health_data.get("timestamp_utc"),
    }))


if __name__ == "__main__":
    asyncio.run(main())
