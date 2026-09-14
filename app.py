from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Query

APP_NAME = "hyperliquid-public-bridge"
APP_VERSION = "1.0.0"
INFO_URL = "https://api.hyperliquid.xyz/info"
PERPS = ("BTC", "ETH", "SOL", "HYPE")
UNKNOWN = "UNKNOWN"
NA = "NOT_APPLICABLE"
ALLOWED = {"allMids", "metaAndAssetCtxs", "spotMetaAndAssetCtxs", "candleSnapshot", "fundingHistory", "l2Book"}
TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=2.0)

app = FastAPI(title="Hyperliquid Public Read-Only Bridge", version=APP_VERSION)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def info(payload: dict[str, Any]) -> Any:
    if payload.get("type") not in ALLOWED:
        raise ValueError("Info type not allowlisted")
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}) as c:
        r = await c.post(INFO_URL, json=payload)
        r.raise_for_status()
        return r.json()


async def safe(payload: dict[str, Any]) -> tuple[Any | None, str | None]:
    try:
        return await info(payload), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:180]}"


def perp_map(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != 2:
        return {}
    meta, ctxs = raw
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    if not isinstance(universe, list) or not isinstance(ctxs, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i, m in enumerate(universe):
        if isinstance(m, dict) and isinstance(m.get("name"), str):
            out[m["name"]] = ctxs[i] if i < len(ctxs) and isinstance(ctxs[i], dict) else {}
    return out


def resolve_xaut(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    meta, ctxs = raw
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None
    tokens = {t.get("index"): t for t in meta.get("tokens", []) if isinstance(t, dict) and isinstance(t.get("index"), int)}
    for pos, pair in enumerate(meta.get("universe", [])):
        if not isinstance(pair, dict):
            continue
        ids = pair.get("tokens")
        if not isinstance(ids, list) or len(ids) != 2:
            continue
        base, quote = tokens.get(ids[0]), tokens.get(ids[1])
        if not base or not quote or base.get("name") != "XAUT0" or quote.get("name") != "USDC":
            continue
        idx = pair.get("index")
        if not isinstance(idx, int):
            continue
        coin = f"@{idx}"
        ctx = next((x for x in ctxs if isinstance(x, dict) and x.get("coin") == coin), None)
        if ctx is None and pos < len(ctxs) and isinstance(ctxs[pos], dict):
            ctx = ctxs[pos]
        return {"pair_index": idx, "api_coin": coin, "base": base, "quote": quote, "ctx": ctx or {}}
    return None


def candle_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, list):
        return {"candles": UNKNOWN, "count": UNKNOWN, "high": UNKNOWN, "low": UNKNOWN}
    candles = []
    highs, lows = [], []
    for c in raw:
        if not isinstance(c, dict):
            continue
        candles.append({"open_time_ms": c.get("t", UNKNOWN), "close_time_ms": c.get("T", UNKNOWN), "interval": c.get("i", UNKNOWN), "open": c.get("o", UNKNOWN), "high": c.get("h", UNKNOWN), "low": c.get("l", UNKNOWN), "close": c.get("c", UNKNOWN), "volume_base": c.get("v", UNKNOWN), "trades": c.get("n", UNKNOWN)})
        try:
            highs.append(float(c["h"])); lows.append(float(c["l"]))
        except Exception:
            pass
    return {"candles": candles, "count": len(candles), "high": str(max(highs)) if highs else UNKNOWN, "low": str(min(lows)) if lows else UNKNOWN, "start_open": candles[0]["open"] if candles else UNKNOWN, "end_close": candles[-1]["close"] if candles else UNKNOWN}


async def aux(coin: str, start_ms: int, end_ms: int) -> dict[str, Any]:
    (candles, ce), (book, be) = await asyncio.gather(
        safe({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms}}),
        safe({"type": "l2Book", "coin": coin}),
    )
    s = candle_summary(candles)
    bid = ask = source_time = UNKNOWN
    if isinstance(book, dict):
        source_time = book.get("time", UNKNOWN)
        levels = book.get("levels")
        if isinstance(levels, list) and len(levels) >= 2:
            if levels[0] and isinstance(levels[0][0], dict): bid = levels[0][0].get("px", UNKNOWN)
            if levels[1] and isinstance(levels[1][0], dict): ask = levels[1][0].get("px", UNKNOWN)
    return {"high_24h": s["high"], "low_24h": s["low"], "best_bid": bid, "best_ask": ask, "source_time_ms": source_time, "errors": [e for e in (ce, be) if e]}


@app.get("/health")
async def health():
    _, err = await safe({"type": "allMids"})
    return {"service": APP_NAME, "version": APP_VERSION, "status": "OK" if err is None else "DEGRADED", "timestamp_utc": now_utc(), "mode": "public-read-only", "upstream": {"provider": "Hyperliquid official public API", "info_endpoint_reachable": err is None, "error": err}, "security": {"wallet_required": False, "private_key_required": False, "user_auth_required": False, "exchange_trading_endpoint_present": False}}


@app.get("/snapshot")
async def snapshot():
    ts = now_utc(); end_ms = int(time.time() * 1000); start_ms = end_ms - 24 * 3600 * 1000
    (pr, pe), (sr, se) = await asyncio.gather(safe({"type": "metaAndAssetCtxs"}), safe({"type": "spotMetaAndAssetCtxs"}))
    perps = perp_map(pr); x = resolve_xaut(sr)
    defs = [(a, a, "perp") for a in PERPS] + ([ ("XAUT0", x["api_coin"], "spot") ] if x else [])
    auxs = await asyncio.gather(*(aux(c, start_ms, end_ms) for _, c, _ in defs)) if defs else []
    amap = {d[0]: a for d, a in zip(defs, auxs)}
    records = []
    for a in PERPS:
        ctx = perps.get(a)
        if ctx is None:
            records.append({"asset": a, "instrument": a, "type": "perp", "status": "UNAVAILABLE", "retrieved_at_utc": ts, "error": pe or "Asset absent"}); continue
        q = amap.get(a, {})
        records.append({"asset": a, "instrument": a, "type": "perp", "status": "PARTIAL" if q.get("errors") else "OK", "retrieved_at_utc": ts, "source_time_ms": q.get("source_time_ms", UNKNOWN), "mark_px": ctx.get("markPx", UNKNOWN), "mid_px": ctx.get("midPx", UNKNOWN), "oracle_index_px": ctx.get("oraclePx", UNKNOWN), "prev_day_px": ctx.get("prevDayPx", UNKNOWN), "high_24h": q.get("high_24h", UNKNOWN), "low_24h": q.get("low_24h", UNKNOWN), "best_bid": q.get("best_bid", UNKNOWN), "best_ask": q.get("best_ask", UNKNOWN), "volume_24h_notional": ctx.get("dayNtlVlm", UNKNOWN), "volume_24h_base": ctx.get("dayBaseVlm", UNKNOWN), "open_interest_base": ctx.get("openInterest", UNKNOWN), "funding_rate_hourly": ctx.get("funding", UNKNOWN), "funding_interval": "1h", "premium": ctx.get("premium", UNKNOWN), "errors": q.get("errors", [])})
    if x:
        ctx = x["ctx"]; q = amap.get("XAUT0", {})
        records.append({"asset": "XAUT0", "instrument": "XAUT0/USDC", "api_coin": x["api_coin"], "spot_pair_index": x["pair_index"], "type": "spot", "status": "PARTIAL" if q.get("errors") else "OK", "retrieved_at_utc": ts, "source_time_ms": q.get("source_time_ms", UNKNOWN), "mark_px": ctx.get("markPx", UNKNOWN), "mid_px": ctx.get("midPx", UNKNOWN), "oracle_index_px": NA, "prev_day_px": ctx.get("prevDayPx", UNKNOWN), "high_24h": q.get("high_24h", UNKNOWN), "low_24h": q.get("low_24h", UNKNOWN), "best_bid": q.get("best_bid", UNKNOWN), "best_ask": q.get("best_ask", UNKNOWN), "volume_24h_notional": ctx.get("dayNtlVlm", UNKNOWN), "volume_24h_base": ctx.get("dayBaseVlm", UNKNOWN), "open_interest_base": NA, "funding_rate_hourly": NA, "funding_interval": NA, "base_token_id": x["base"].get("tokenId", UNKNOWN), "quote_token_id": x["quote"].get("tokenId", UNKNOWN), "identity_guard": "Exact Hyperliquid spot XAUT0/USDC; never xyz:GOLD", "errors": q.get("errors", [])})
    else:
        records.append({"asset": "XAUT0", "instrument": "XAUT0/USDC", "type": "spot", "status": "UNAVAILABLE", "retrieved_at_utc": ts, "error": se or "Exact XAUT0/USDC pair not found", "identity_guard": "No fallback to xyz:GOLD"})
    status = "OK" if records and all(r.get("status") == "OK" for r in records) else "PARTIAL" if any(r.get("status") in ("OK", "PARTIAL") for r in records) else "UNAVAILABLE"
    return {"service": APP_NAME, "version": APP_VERSION, "timestamp_utc": ts, "status": status, "assets": records, "upstream_errors": {"perps": pe, "spot": se}}


@app.get("/history")
async def history(hours: int = Query(default=2, ge=1, le=24)):
    ts = now_utc(); end_ms = int(time.time() * 1000); start_ms = end_ms - hours * 3600 * 1000; interval = "1m" if hours <= 6 else "5m"
    (pr, pe), (sr, se) = await asyncio.gather(safe({"type": "metaAndAssetCtxs"}), safe({"type": "spotMetaAndAssetCtxs"}))
    perps = perp_map(pr); x = resolve_xaut(sr)
    defs = [(a, a, "perp") for a in PERPS] + [("XAUT0", x["api_coin"] if x else "", "spot")]
    async def one(asset: str, coin: str, kind: str):
        inst = asset if kind == "perp" else "XAUT0/USDC"
        if not coin:
            return {"asset": asset, "instrument": inst, "type": kind, "status": "UNAVAILABLE", "candles": UNKNOWN, "funding_history": NA if kind == "spot" else UNKNOWN, "oi_history": NA if kind == "spot" else UNKNOWN, "error": se or "Exact XAUT0/USDC pair unresolved"}
        cp = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}}
        if kind == "perp":
            (cr, ce), (fr, fe) = await asyncio.gather(safe(cp), safe({"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}))
        else:
            cr, ce = await safe(cp); fr, fe = NA, None
        s = candle_summary(cr); errs = [e for e in (ce, fe) if e]
        oi = perps.get(asset, {}).get("openInterest", UNKNOWN) if kind == "perp" else NA
        return {"asset": asset, "instrument": inst, "api_coin": coin, "type": kind, "status": "PARTIAL" if errs else "OK", "interval": interval, "start_time_ms": start_ms, "end_time_ms": end_ms, "candle_count": s.get("count", UNKNOWN), "path_summary": {"start_open": s.get("start_open", UNKNOWN), "end_close": s.get("end_close", UNKNOWN), "high": s.get("high", UNKNOWN), "low": s.get("low", UNKNOWN)}, "candles": s.get("candles", UNKNOWN), "current_open_interest_base": oi, "oi_history": UNKNOWN if kind == "perp" else NA, "oi_history_note": "No official documented public Info REST endpoint exposes historical OI series; not fabricated." if kind == "perp" else NA, "funding_history": fr if kind == "perp" and fr is not None else (UNKNOWN if kind == "perp" else NA), "funding_interval": "1h" if kind == "perp" else NA, "errors": errs}
    records = await asyncio.gather(*(one(*d) for d in defs))
    status = "OK" if all(r.get("status") == "OK" for r in records) else "PARTIAL" if any(r.get("status") in ("OK", "PARTIAL") for r in records) else "UNAVAILABLE"
    return {"service": APP_NAME, "version": APP_VERSION, "timestamp_utc": ts, "status": status, "requested_hours": hours, "interval": interval, "assets": records, "upstream_errors": {"perps": pe, "spot": se}}


@app.get("/xaut")
async def xaut():
    ts = now_utc(); end_ms = int(time.time() * 1000); start_ms = end_ms - 24 * 3600 * 1000
    sr, se = await safe({"type": "spotMetaAndAssetCtxs"}); x = resolve_xaut(sr)
    if not x:
        return {"service": APP_NAME, "version": APP_VERSION, "timestamp_utc": ts, "status": "UNAVAILABLE", "instrument": "XAUT0/USDC", "type": "spot", "identity_guard": "No fallback to xyz:GOLD or any other gold instrument", "error": se or "Exact XAUT0/USDC pair not found"}
    q = await aux(x["api_coin"], start_ms, end_ms); ctx = x["ctx"]
    return {"service": APP_NAME, "version": APP_VERSION, "timestamp_utc": ts, "status": "PARTIAL" if q.get("errors") else "OK", "instrument": "XAUT0/USDC", "type": "spot", "api_coin": x["api_coin"], "spot_pair_index": x["pair_index"], "base": {"name": x["base"].get("name", UNKNOWN), "token_index": x["base"].get("index", UNKNOWN), "token_id": x["base"].get("tokenId", UNKNOWN)}, "quote": {"name": x["quote"].get("name", UNKNOWN), "token_index": x["quote"].get("index", UNKNOWN), "token_id": x["quote"].get("tokenId", UNKNOWN)}, "market": {"source_time_ms": q.get("source_time_ms", UNKNOWN), "mark_px": ctx.get("markPx", UNKNOWN), "mid_px": ctx.get("midPx", UNKNOWN), "prev_day_px": ctx.get("prevDayPx", UNKNOWN), "high_24h": q.get("high_24h", UNKNOWN), "low_24h": q.get("low_24h", UNKNOWN), "best_bid": q.get("best_bid", UNKNOWN), "best_ask": q.get("best_ask", UNKNOWN), "volume_24h_notional": ctx.get("dayNtlVlm", UNKNOWN), "volume_24h_base": ctx.get("dayBaseVlm", UNKNOWN), "open_interest": NA, "funding": NA}, "identity_guard": {"required_base": "XAUT0", "required_quote": "USDC", "forbidden_substitution": "xyz:GOLD", "resolution": "dynamic from official spotMeta tokens + universe"}, "errors": q.get("errors", [])}
