from dataclasses import dataclass


@dataclass
class Order:
    id: int
    customer: str


def list_orders(orders: list[Order]) -> list[Order]:
    return sorted(orders, key=lambda o: o.id)
