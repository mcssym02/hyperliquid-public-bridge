# Public Crypto Read-Only Data Collector

Static JSON collectors for public crypto market/state data. GitHub Actions periodically queries public endpoints and Base JSON-RPC, then commits stable JSON files under `data/` for anonymous read-only access through GitHub Pages.

There is **no hosted trading server** and no private wallet integration.

## Security model

- no private key
- no seed phrase
- no wallet connection
- no API secret for Base/Uniswap
- no signatures
- no transaction broadcast
- no swaps, deposits, withdrawals, claims, reranges or trades
- Base LP collector uses only `eth_chainId` and `eth_call`
- the fee read is an `eth_call` simulation of `collect()`; the simulated state is discarded and nothing is broadcast

## Hyperliquid market collector

Uses Hyperliquid public market APIs for:

- BTC perpetual
- ETH perpetual
- SOL perpetual
- HYPE perpetual
- exact **XAUT0/USDC spot** only

Generated files include:

- `data/health.json`
- `data/snapshot.json`
- `data/history-2h.json`
- `data/xaut.json`
- `data/liquidations.json`
- `data/derived.json`
- `data/radar-core.json`
- `data/market-state-history.json`

## Base / Uniswap V3 LP collector

`collect_uniswap_lp.py` reads the current public on-chain state for:

- Network: **Base (8453)**
- Uniswap V3 NonfungiblePositionManager: `0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1`
- Uniswap V3 Factory: `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`
- NFT position: **#6000457**
- Expected owner/state wallet: `0xf6c62b83f742246080bb42a3e2512474bc690ed1`

It publishes:

- `data/uniswap-lp.json`

The JSON includes, when the public RPC calls succeed:

- on-chain NFT owner and identity guard
- token0/token1 and fee tier
- tick lower / tick upper
- pool address
- current pool tick and `sqrtPriceX96`
- exact range state: `IN RANGE`, `OUT OF RANGE — HAUT`, or `OUT OF RANGE — BAS`
- current pool price
- principal composition calculated from on-chain liquidity and pool state
- estimated USDC value for WETH/USDC
- collectable fee quote from a read-only `eth_call` simulation
- timestamps and source/method fields

If the NFT owner no longer matches the expected active-state wallet, validation fails rather than silently reusing the old range.

## RPC policy

The LP collector tries public Base RPC endpoints in order:

1. `https://mainnet.base.org`
2. `https://base-rpc.publicnode.com`
3. `https://base.llamarpc.com`

No RPC key or user secret is required.

## Schedule

`.github/workflows/refresh-data.yml` runs at minute `45` and `55` of every hour, plus manually and after collector-code changes.

The workflow validates identities and safety invariants before committing refreshed data. A failed Base/LP identity check does not get treated as confirmed portfolio STATE.

## Publication

GitHub Pages base:

`https://mcssym02.github.io/hyperliquid-public-bridge/`

LP endpoint:

`https://mcssym02.github.io/hyperliquid-public-bridge/data/uniswap-lp.json`
