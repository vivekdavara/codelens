from dataclasses import dataclass


@dataclass
class Message:
    to: str
    subject: str
    body: str


def welcome(address: str, name: str) -> Message:
    return Message(address, "Welcome!", f"Hi {name}, thanks for signing up.")
