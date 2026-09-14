# Hyperliquid Public Read-Only Data Collector

Static JSON collector over Hyperliquid's official public `POST /info` API.

There is **no hosted bridge/server** in this architecture. GitHub Actions periodically collects public market data and writes stable JSON files under `data/`.

## Security model

- no wallet
- no private key
- no Hyperliquid API secret
- no user authentication
- no signatures
- no trading endpoint
- no call to `/exchange`
- read-only public Hyperliquid market data only

## Priority instruments

- BTC perpetual
- ETH perpetual
- SOL perpetual
- HYPE perpetual
- exact **XAUT0/USDC spot** only

XAUT0 is resolved dynamically from official spot metadata by matching base `XAUT0` and quote `USDC`, then using Hyperliquid's `@<spot_pair_index>` API identifier. The collector never substitutes `xyz:GOLD` or another gold instrument.

## Generated files

- `data/health.json`
- `data/snapshot.json`
- `data/history-2h.json`
- `data/xaut.json`

Missing upstream data is represented as `UNKNOWN`; structurally inapplicable fields use `NOT_APPLICABLE`.

## Schedule

`.github/workflows/refresh-data.yml` runs at minute `45` and `55` of every hour, plus manually and after collector-code changes. This is intentionally positioned shortly before the Trading Radar hourly run.

The workflow validates that all four JSON files exist, contain timestamps, and that the XAUT output identifies **XAUT0/USDC**. Generated data is committed back to the repository.

## Current publication state

The repository can remain private while the collector is being validated. For a Radar that must read the JSON through anonymous public HTTP GET requests, the generated JSON must later be exposed through a public read-only URL. No Radar integration should be enabled until the first successful workflow output has been inspected and validated.
