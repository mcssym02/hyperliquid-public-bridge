"""Offline continuous V3 principal model in human base/quote units.

No RPC, files, wallet, persistence, J0 selection, fees or USD peg inference.
Virtual liquidity is NOT the pool's raw integer liquidity. This model omits
integer mint/burn rounding and measures principal only, not executable returns.
Equations: Uniswap v3-periphery/contracts/libraries/LiquidityAmounts.sol.
"""
from decimal import Decimal, InvalidOperation, localcontext
from functools import wraps


def precise(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 70
            return function(*args, **kwargs)
    return wrapped


def number(value, *, zero=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("use finite decimal strings, integers or Decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal input") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise ValueError("invalid amount or price")
    return result


@precise
def bounds_from_ticks(lower, upper, decimals0, decimals1, *, base_is_token0, spacing):
    if any(type(x) is not int for x in (lower, upper, decimals0, decimals1, spacing)):
        raise ValueError("ticks, decimals and spacing must be integers")
    if type(base_is_token0) is not bool:
        raise ValueError("explicit token orientation required")
    if not -887272 <= lower < upper <= 887272 or not 1 <= spacing <= 16384:
        raise ValueError("invalid ticks or spacing")
    if lower % spacing or upper % spacing or not all(0 <= d <= 255 for d in (decimals0, decimals1)):
        raise ValueError("unaligned ticks or invalid decimals")
    prices = [Decimal('1.0001') ** t * Decimal(10) ** (decimals0-decimals1) for t in (lower, upper)]
    if not base_is_token0:
        prices = [1/p for p in prices]
    return tuple(sorted(prices))


@precise
def amounts(price, lower, upper, liquidity):
    """Return human base and quote principal; endpoints have zero-width overlap."""
    p, a, b, liq = number(price), number(lower), number(upper), number(liquidity, zero=True)
    if a >= b:
        raise ValueError("range must increase in quote/base units")
    sa, sb = a.sqrt(), b.sqrt()
    s = min(max(p, a), b).sqrt()
    return liq * (1/s - 1/sb), liq * (s-sa)


@precise
def hypothetical_allocation(price, lower, upper, budget_quote):
    """Pure what-if allocation. It never establishes or selects an experiment J0."""
    p, budget = number(price), number(budget_quote)
    x, y = amounts(p, lower, upper, 1)
    liq = budget/(x*p+y)
    x, y = amounts(p, lower, upper, liq)
    return dict(liquidity=liq, base=x, quote=y, base_value_share=x*p/budget,
                model='CONTINUOUS_HYPOTHETICAL_QUOTE_UNITS', j0_initialized=False)


@precise
def reconcile_frozen(price0, lower, upper, base0, quote0):
    """Check that frozen quantities describe one L; never replace those quantities.

    Relative tolerance 1e-6 accepts published quantity rounding, not a changed
    baseline. For any material inconsistency the model stops. The lesser implied
    L is used and the full original quantities remain the HOLD benchmark.
    """
    p, x0, y0 = number(price0), number(base0, zero=True), number(quote0, zero=True)
    x1, y1 = amounts(p, lower, upper, 1)
    implied = []
    for recorded, per_liquidity in ((x0,x1), (y0,y1)):
        if per_liquidity == 0:
            if recorded != 0:
                raise ValueError("frozen composition disagrees with initial range state")
        else:
            implied.append(recorded/per_liquidity)
    if not implied or min(implied) <= 0:
        raise ValueError("empty frozen allocation")
    gap = max(implied)/min(implied)-1
    if gap > Decimal('0.000001'):
        raise ValueError("inconsistent frozen composition; no automatic repair")
    return dict(liquidity=min(implied), implied_liquidity_relative_gap=gap)


@precise
def principal_snapshot(price0, lower, upper, base0, quote0, price):
    """Report fixed-composition HOLD and LP in quote units, excluding all income."""
    p0, p = number(price0), number(price)
    a, b = number(lower), number(upper)
    x0, y0 = number(base0, zero=True), number(quote0, zero=True)
    reconciliation = reconcile_frozen(p0, a, b, x0, y0)
    x, y = amounts(p, a, b, reconciliation['liquidity'])
    principal, hold = x*p+y, x0*p+y0
    initial = x0*p0+y0
    return dict(**reconciliation, base=x, quote=y, principal_quote=principal,
                hold_quote=hold, divergence_quote=principal-hold,
                initial_value_quote=initial, principal_change_quote=principal-initial,
                range_status='OUT_LOW' if p <= a else 'OUT_HIGH' if p >= b else 'IN_RANGE',
                economic_scope='CONTINUOUS_PRINCIPAL_ONLY_QUOTE_UNITS',
                # Endpoint labels mean mono-asset principal; protocol tick-based
                # active-liquidity membership must be checked separately.
                usd_value=None, position_fees=None, rewards=None, costs=None,
                net_return=None, time_in_range=None, usd_peg_assumed=False)
