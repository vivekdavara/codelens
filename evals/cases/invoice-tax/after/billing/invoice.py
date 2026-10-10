from dataclasses import dataclass

TAX_RATE = 0.0825


@dataclass
class Item:
    name: str
    price_cents: int
    quantity: int


def subtotal_cents(items: list[Item]) -> int:
    return sum(item.price_cents * item.quantity for item in items)


def tax_cents(items: list[Item]) -> int:
    return round(subtotal_cents(items) * TAX_RATE / 100) * 100


def parse_amount(text: str) -> int | None:
    try:
        return int(round(float(text.strip().lstrip("$")) * 100))
    except (ValueError or TypeError):
        return None
