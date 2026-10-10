from decimal import Decimal

TAX_RATE = Decimal("0.08")


def line_total(price: Decimal, quantity: int) -> Decimal:
    return price * quantity


def order_total(lines: list[tuple[Decimal, int]]) -> Decimal:
    subtotal = sum((line_total(p, q) for p, q in lines), Decimal("0"))
    return (subtotal * (1 + TAX_RATE)).quantize(Decimal("0.01"))
