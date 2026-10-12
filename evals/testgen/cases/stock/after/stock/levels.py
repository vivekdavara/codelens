"""Reorder points for a small warehouse."""


def reorder_point(daily_demand, lead_days, safety_days=0):
    """The stock level at which to reorder: demand until the delivery arrives, plus a safety buffer."""
    if daily_demand < 0 or lead_days < 0 or safety_days < 0:
        raise ValueError("demand and days must not be negative")
    if daily_demand == 0:
        return 0
    return daily_demand * (lead_days + safety_days)


def parse_sku(text):
    """A SKU like "AB-1234", as printed on a shelf label; case and surrounding spaces don't matter."""
    cleaned = text.strip().upper()
    if cleaned.count("-") != 1:
        raise ValueError(f"not a SKU: {text!r}")
    prefix, number = cleaned.split("-")
    if not prefix.isalpha() or not number.isdigit():
        raise ValueError(f"not a SKU: {text!r}")
    return prefix, int(number)


def units_to_order(on_hand, daily_demand, lead_days, safety_days=0, pack_size=1):
    """Units to order now, rounded up to whole packs; 0 while stock is above the reorder point."""
    point = reorder_point(daily_demand, lead_days, safety_days)
    if on_hand > point:
        return 0
    short = point - on_hand
    packs = -(-short // pack_size)
    return packs * pack_size
