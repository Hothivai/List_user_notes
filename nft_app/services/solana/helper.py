def tick_to_price(tick: int, decimals_mint_0: int, decimals_mint_1: int) -> float:
    return (1.0001 ** tick) * (10 ** (decimals_mint_0 - decimals_mint_1))

def filter_price_ranges(ranges, min_price=1e-20, max_price=1e20):
    return [
        r for r in ranges
        if min_price <= r["price_low"] <= max_price
        and min_price <= r["price_up"] <= max_price
    ]
