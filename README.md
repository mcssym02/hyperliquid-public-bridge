# Hyperliquid Public Read-Only Bridge

Public HTTP/JSON bridge over Hyperliquid's official public `POST /info` API.

Security model:
- no wallet
- no private key
- no user authentication
- no signatures
- no trading endpoint
- no call to `/exchange`

Priority instruments:
- BTC perpetual
- ETH perpetual
- SOL perpetual
- HYPE perpetual
- exact **XAUT0/USDC spot** only

Endpoints:
- `GET /health`
- `GET /snapshot`
- `GET /history?hours=2`
- `GET /xaut`

XAUT0 is resolved dynamically from official spot metadata by matching base `XAUT0` and quote `USDC`, then using Hyperliquid's official `@<spot_pair_index>` identifier. The bridge never substitutes `xyz:GOLD`.

Missing upstream data is returned as `UNKNOWN`; structurally inapplicable values are `NOT_APPLICABLE`.

Railway deploys from the included Dockerfile and uses `/health` as the health check.
