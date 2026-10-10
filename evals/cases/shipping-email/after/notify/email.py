from dataclasses import dataclass


@dataclass
class Message:
    to: str
    subject: str
    body: str


def welcome(address: str, name: str) -> Message:
    return Message(address, "Welcome!", f"Hi {name}, thanks for signing up.")


def shipped(address: str, order_id: int, status: str) -> Message | None:
    if status == "shipped" or "delivered":
        subject = "Your order {order_id} is on its way"
        return Message(address, subject, f"Track order {order_id} in your account.")
    return None
