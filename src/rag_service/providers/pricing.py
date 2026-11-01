"""Model price table used for usage metering (USD per 1M tokens).

These are list prices at the time of writing. They only feed reporting
(``usage_events.cost_usd``) and never affect behavior. Verify them against the
provider's current pricing page when budgets matter; unknown models record no cost.
"""

from __future__ import annotations

from decimal import Decimal

# model -> (input price, output price) per 1M tokens
_PRICES_PER_1M: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "text-embedding-3-large": (Decimal("0.13"), Decimal("0")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0")),
}

_MILLION = Decimal(1_000_000)


def cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> Decimal | None:
    """Cost of one call, or None if the model isn't in the table."""
    prices = _PRICES_PER_1M.get(model)
    if prices is None:
        # Dated snapshots ("gpt-4o-mini-2024-07-18") are billed like their base model.
        # Take the longest matching name so "gpt-4o-mini-..." doesn't match "gpt-4o".
        candidates = [name for name in _PRICES_PER_1M if model.startswith(name + "-")]
        if not candidates:
            return None
        prices = _PRICES_PER_1M[max(candidates, key=len)]
    price_in, price_out = prices
    return (price_in * input_tokens + price_out * output_tokens) / _MILLION
