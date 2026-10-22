# Exact PAPER pool source verification

Status: deployed on 2026-10-22 after explicit user approval of PR #2.
The proposed change reuses the existing main-branch refresh workflow and its
10-minute cron; it creates no additional recurring task or job. It changes no
automation prompt, capital document, PAPER register or historical J0. PR CI has
`contents: read` and cannot publish to main.

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
3. Before merging, approve the concrete workflow delta below, including its
   bounded runtime/fallback, source availability and retention plan.
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

## Reviewed recurring integration — approved and applied

- Keep the existing `*/10 * * * *` cron, main-only publication guards, tracked
  NFT/owner, primary collectors and 4-minute job limit unchanged.
- After the existing collectors, run `refresh_paper_pools.py` once. Its subprocess
  receives a hard 20-second timeout; source timeout/crash/invalid JSON becomes
  explicit ERROR with no usable prices and does not fail the optional collector.
- Validate identity, quote units, fixed tiers, freshness (source block age at most
  180 seconds at publication), counts, coverage and null economic fields. A partial
  valid snapshot retains only verified pool prices. Never reuse last cycle prices.
- Publish `data/paper-pools.json` through the existing data commit/Pages flow.
  Only public pool data is included: no account, holdings, balance, PAPER capital,
  simulated position or private canonical document. Consumers must check status,
  each block timestamp, coverage and synchronization before use.
- Add the two pool scripts to the existing push path filter. Change no trigger
  timing and create no extra recurring workflow. Add up to 20 seconds plus small
  startup/JSON overhead to a refresh; availability is not guaranteed by a
  successful PR check. Public RPC rate limits remain an operational limitation.
- Retain the latest feed in git history through the existing publication process;
  keep the initial 22 October checkpoint unchanged. PR evidence artifacts expire
  after 7 days. No additional rolling volume/history dataset is introduced.

This feed supplies observed spot ratios only. Formula reconstruction, fee models,
LPWIDTH initialization and the deferred Google Docs writer incident stay open.

Rollback before merge: close the draft PR; production is unchanged. After merge,
remove the optional refresh step, pool JSON validation/required-list entry,
`data/paper-pools.json` from the commit whitelist, and the two scripts from the
push path filter. Preserve all other collectors, cron and main-only guards. Keep
historical checkpoints. Before leaving any old pool JSON available, replace it
with explicit disabled/ERROR metadata and no usable prices in the same reviewed
rollback, so consumers cannot mistake it for fresh data.


## Production verification — 2026-10-22

PR #2 merged as `23f14505fab24049e3abc6e494fc5e16c6024d41`, after both CI
checks passed at source head `da544890ceadf46f269f9dfc7b15045289af904a`
(35 deterministic tests plus live source and NFT checks).

First production refresh `39869572464` / job `120140293817` succeeded. Data
commit `0b1af6baf1e18a04f0b5208948b688a060354845` was published by successful
Pages run `39869728863`. Readback of `data/paper-pools.json` at
`2026-10-22T23:07:42.873170Z` verified five identities and complete coverage.
The optional refresh took about 2.8 seconds in that run, with its configured
20-second limit. Radar core, configured LP and scoped NFT inventory also read OK.
This verifies the first cycle only, without guaranteeing future RPC availability.
The Google Docs writer incident remains unresolved and deferred separately.
