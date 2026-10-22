# Exact PAPER pool source verification

Status: preparation only. This pull request does not change the scheduled refresh
workflow, an automation prompt, a capital document, or a PAPER register. Its CI
has `contents: read` and runs only on relevant pull requests; no new recurrence.

## Problem and scope

The existing discovery feed cannot prove that the five exact cohort pools were
collected. `collect_paper_pools.py` observes their own pool contracts on Base 8453
and Robinhood Chain 4663, independently. It checks chain, token ordering,
decimals, fixed fee/spacing where declared, valid initialized price and a final
unchanged block header. A failed pool remains unavailable; a price from another
pool is never substituted. A changed block invalidates its entire chain result.

Fields are spot ratios in their actual quote tokens, not USD. Fee is the current
swap fee in millionths, not position income. Slipstream fee is observed at the
pinned block and may change. Active raw liquidity is not TVL or a depth quote.

No historical J0, range, composition, missing period, fees, reward, cost or net
return is created. No wallet balances or private registry contents are published.
LPWIDTH initialization is outside this collector's scope.

## Sources

- Pool identities: fixed project cohort; live contract checks are still required.
- Uniswap ABI: https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolState.sol
- Uniswap immutable getters: https://github.com/Uniswap/v3-core/blob/main/contracts/interfaces/pool/IUniswapV3PoolImmutables.sol
- Slipstream ABI: https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/interfaces/pool/ICLPoolState.sol
- Slipstream getters: https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/interfaces/pool/ICLPoolConstants.sol
- Robinhood network and public endpoint: https://docs.robinhood.com/chain/connecting/

Robinhood documents its public RPC as rate-limited and unsuitable for production.
A successful one-time test does not settle production capacity, liveness or an
endpoint contract. No paid provider, credential or key is provisioned here.

## Validation and proposed integration gate

1. Run deterministic tests and verify all five sources in PR CI; inspect individual
   failures and preserve the observation artifact even when coverage fails.
2. Review block freshness, cross-chain skew, identity and unit semantics. Separate
   per-chain pinned blocks are not an atomic cross-chain or finalized snapshot.
3. Before any recurring integration, present the concrete workflow delta, bounded
   runtime/fallback, source availability and retention plan for human approval.
4. Keep economic formula reconstruction and persistence safety incident separate.
   Do not retry a blocked writer through this collector or another mechanism.

Reproduction: `python -m unittest discover -s tests -v`, then
`python collect_paper_pools.py --output paper-pool-check.json`.
The collector returns 2 for incomplete coverage and writes explicit nulls/errors.

## Initial live verification — 2026-10-22

Source commit `5e862950a859cdf63a15168fb671de1ec8ea0330` passed all 26 tests
locally and in CI. Both PR checks succeeded, including the existing scoped NFT
integrity check. Exact-pool run: `39840593155`; job: `120040258488`.

The market observation at `2026-10-22T19:29:58.505548Z` verified 5/5 identities.
Base block `52954024` and Robinhood block `95355006` both had timestamp
`2026-10-22T19:29:55Z`; cross-chain timestamp skew was zero, without implying
atomicity. Slipstream reported fee 500 millionths and tick spacing 50 at that
block. All unavailable economic fields remain null. The live network reads took
about three seconds in this test; this is not an availability or runtime SLA.

The unchanged observation payload is retained at
`data/checkpoints/paper-pools-20261022.json`. Original artifact `12452762369`
had ZIP SHA256 `bb3b4fe1f7d0ca605876a478e09d64fabb3b34ab49979fdaf8326411449ab940`;
that hash covers the archive, not the extracted JSON. The checkpoint is historical,
not a freshness feed or a new prospective PAPER J0.

Rollback at this preparation stage: close the draft PR. No production deployment
or scheduled behavior needs reverting. Any future integration must have its own
reviewed rollback delta.
