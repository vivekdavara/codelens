from dataclasses import dataclass


@dataclass
class Order:
    id: int
    customer: str


def list_orders(orders: list[Order]) -> list[Order]:
    return sorted(orders, key=lambda o: o.id)


def page_count(total: int, size: int) -> int:
    return total // size


def page(orders: list[Order], number: int, size: int = 20) -> list[Order]:
    """Orders on page ``number`` (1-based)."""
    ordered = list_orders(orders)
    start = (number - 1) * size
    return ordered[start : start + size + 1]
