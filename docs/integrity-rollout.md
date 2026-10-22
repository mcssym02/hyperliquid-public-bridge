# LP inventory and PAPER integrity: staged rollout

This draft leaves production unchanged until integration. Its proposed refresh
workflow adds a bounded public-read inventory step and JSON output, preserving the
existing cadence, market collectors and selected NFT. No portfolio balance,
automation destination, historical PAPER result, wallet authentication or
transaction endpoint is changed.

## LP activity guard

The tracked NFT remains configured by UNISWAP_NFT_ID. Identity and positive
liquidity are separate facts. A verified owner with zero liquidity must not be
advertised as an active LP. Stored tokens owed are distinguished from active
liquidity; they may include principal and are not automatically called fees.
The existing safe_for_automatic_state_update field requires positive liquidity;
it remains an observation guard, never permission to change financial STATE.

## Owned-NFT discovery

Run `python discover_uniswap_positions.py --owner 0x... --output inventory.json`
against public Base RPC. The inventory pins all reads to one block, verifies chain,
enumerated token IDs, owner and position data, and reports partial coverage on a
cap, duplicate, RPC error or identity mismatch. An incomplete inventory reports
active_nft_ids=null, with observed discoveries separately. It never selects a new
tracked NFT. Empty inventory means no directly owned NFT in the declared manager
and block, not no DeFi exposure. Other wallets/chains/managers/custody/staking are
outside scope.

A live PR check verified all 33 directly owned NFTs at block 52953413 on
2026-10-22T19:09:34Z; none had positive liquidity. The exact historical observation
is preserved in data/checkpoints/uniswap-owned-positions-20261022.json. This does
not prove absence of other-account, other-manager or custody/staking exposure.
The proposed refresh step keeps tracked-NFT valuation separate, caps inventory at
60 seconds and emits explicit ERROR/null activity on failure. Inventory errors
do not block primary market publication. Before promotion, review the workflow
diff and schema validation. Cadence remains unchanged.

## PAPER inventory

`python audit_paper_registry.py ledger.txt --now 2026-10-22T15:50:00-03:00`
recognizes SNAPSHOT, SNAPSHOT_PAPER, RUN, bare RADAR IDs and RUN_ID legacy headers,
and both EVAL formats. It counts records, detects duplicates, missing pairs,
orphans and malformed IDs. It never evaluates a trade, rewrites a historical ID,
infers a scheduled run from an absent hour or authorizes archive deletion.

The input must be independently verified complete. Prefer a full native document
read with body indexes and content, or a complete text export. Text-only responses
can truncate the last paragraph. Audit results must include revision/hash/as-of;
counts from different revisions or record families are not comparable. Old prose
without stable IDs is preserved and excluded from numeric claims.

## Rotation gate

Create a full immutable backup first. Build a manifest of exact original blocks;
preserve all rules, schema, unresolved incidents, malformed entries and backlog.
Only a fully evaluated pair of horizons is an initial eligibility criterion, not
sufficient permission to delete. Keep recent runs in the working document. Read
back the archive, hashes, ID sets and source revision before any removal; stop on
concurrent append. Do not rotate while reliable archive writes are unproven.

## Validation and rollback

Run `python -m unittest discover -s tests -v` and compile the collectors. The unit
tests use a deterministic RPC simulator; they do not prove live RPC access or
complete portfolio coverage. A new branch/PR preserves production until checks
pass. Roll back code with a revert of the integration commit, without rewriting
data history. Do not add real wallet state, Google document text or private audit
inputs to this public repository.
