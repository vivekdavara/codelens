from dataclasses import dataclass


@dataclass
class Item:
    name: str
    price_cents: int
    quantity: int


def subtotal_cents(items: list[Item]) -> int:
    return sum(item.price_cents * item.quantity for item in items)
