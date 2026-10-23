# Offline PAPER principal model

`paper_lp_math.py` is a standalone, dependency-free Decimal model. It has no
network or file access, no CLI, no wallet, no automatic caller and no writer.
Adding it does not change scheduled collectors, task prompts, experiment J0 or
historical compositions. It is preparation for a separately reviewed integration.

For price P in quote tokens per base token, let a=sqrt(lower), b=sqrt(upper),
and s=sqrt(clamp(P, lower, upper)). Continuous human-token principal is
`base = L * (1/s - 1/b)` and `quote = L * (s-a)`. This virtual L must never be
compared with the pool's raw active liquidity or used as a fee share. It omits
integer mint/burn rounding, fees, rewards, protocol-specific mechanics and costs.

Tick conversion uses `1.0001**tick * 10**(decimals0-decimals1)` for token1/token0
in human units; if base is token1, invert and sort both bounds. Token identity,
orientation, spacing, decimals and source freshness must be verified by callers.
The helper validates arithmetic inputs; it does not authenticate a pool or feed.
Snapshot range labels describe mono-asset endpoints, not exact protocol
active-liquidity membership at a tick boundary.

`hypothetical_allocation` uses an explicit quote-token budget and never selects
or initializes J0. A USD budget requires separately observed token/USD prices.
No USDC, USDG or other stablecoin peg is inferred. An out-of-range initial price
correctly produces a mono-asset allocation; different widths need not have the
same initial exposure even when they share a center and a nominal budget.

`reconcile_frozen` checks the two implied liquidities against a fixed 1e-6 relative
tolerance for published quantity rounding. It rejects material disagreement and
never repairs or replaces the frozen quantities. The smaller implied L is used
as a continuous approximation, while HOLD retains both original quantities.
The discrepancy is returned explicitly, not silently treated as exact history.
Do not change historical ticks or J0 to match a current tick spacing. An experiment
defined only by continuous price bounds remains a continuous approximation.

`principal_snapshot` returns LP principal, fixed-composition HOLD and divergence
in quote tokens. USD value, fees, rewards, costs, net return and elapsed time in
range remain null. Sparse snapshots do not establish continuous time in range.
Neither APR nor pool TVL/volume can be substituted for attributable position fees.
Historical data gaps and asynchronous baseline prices retain their uncertainty.
These functions do not validate execution price, jurisdiction, protocol access,
reference NFT, or the prerequisites for starting an experiment.

Equations checked against the official Uniswap implementation:
- https://github.com/Uniswap/v3-periphery/blob/main/contracts/libraries/LiquidityAmounts.sol
- File blob observed during review: `14270224bf9b8c3191d4ed92f8772ab8f1ac5a7f`.

Validation: `python -m unittest discover -s tests -p 'test_*.py' -q`.
The model tests cover independent analytic vectors, below/inside/above range,
fixed HOLD, budget conservation, reciprocal prices and token decimals, malformed
inputs, frozen-composition disagreement and precision independent of the caller.
They do not certify on-chain valuation, future availability or economic returns.
