"""Pure activity classification shared by read-only LP collectors."""

def classify_position(liquidity: int, owed0: int = 0, owed1: int = 0) -> dict:
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in (liquidity, owed0, owed1)):
        raise ValueError("liquidity and token amounts must be nonnegative integers")
    active = liquidity > 0
    status = "ACTIVE_LIQUIDITY" if active else (
        "NO_LIQUIDITY_TOKENS_OWED" if owed0 or owed1 else "EMPTY_POSITION")
    return {"has_active_liquidity": active, "activity_status": status}
