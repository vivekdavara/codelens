"""Reorder points for a small warehouse."""


def reorder_point(daily_demand, lead_days):
    """The stock level at which to reorder: enough to cover demand until the delivery arrives."""
    return daily_demand * lead_days


def parse_sku(text):
    """A SKU like "AB-1234", as printed on a shelf label."""
    prefix, number = text.split("-")
    return prefix, int(number)
