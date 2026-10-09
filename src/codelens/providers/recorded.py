"""Replay saved model responses, keyed by a hash of the request, so tests and CI never call a vendor.

A recording is one JSON file named ``<key>.json`` holding the request it answers (so it can be checked and
read by a person), the response text, the model that wrote it and the token usage. Recordings written by hand
for tests say ``"model": "hand-written"``; they exercise the pipeline and are never reported as model quality.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codelens.providers.base import Completion, Provider, ProviderError, RecordingMissing, Request, Usage

__all__ = ["HAND_WRITTEN", "RecordedProvider", "Recorder", "load_recording", "write_recording"]

HAND_WRITTEN = "hand-written"


def write_recording(directory: Path, request: Request, completion: Completion, provider: str) -> Path:
    """Save ``completion`` as the answer to ``request``; returns the file written."""
    directory.mkdir(parents=True, exist_ok=True)
    key = request.key()
    record: dict[str, Any] = {
        "key": key,
        "provider": provider,
        "model": completion.model,
        "system": request.system,
        "prompt": request.prompt,
        "schema": request.schema,
        "response": completion.text,
        "usage": {
            "input_tokens": completion.usage.input_tokens,
            "output_tokens": completion.usage.output_tokens,
        },
    }
    path = directory / f"{key}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_recording(path: Path) -> tuple[Request, Completion]:
    """Read a recording and check that its stored request still hashes to its file name."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        request = Request(record["system"], record["prompt"], record["schema"])
        usage = Usage(**record.get("usage", {}))
        completion = Completion(record["response"], record["model"], usage)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(f"unreadable recording {path}: {exc}") from None
    if request.key() != path.stem or record.get("key") != path.stem:
        raise ProviderError(f"recording {path} does not match its key (was it edited by hand?)")
    return request, completion


class RecordedProvider:
    """Answers only requests it has a recording for; anything else is a loud :class:`RecordingMissing`."""

    name = "recorded"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def complete(self, request: Request) -> Completion:
        path = self.directory / f"{request.key()}.json"
        if not path.exists():
            raise RecordingMissing(
                f"no recorded response for this request in {self.directory} (expected {path.name}); "
                "the prompt or schema changed, or it was never recorded: record it with a live provider "
                "(codelens review --record)"
            )
        return load_recording(path)[1]


class Recorder:
    """Wraps a live provider and saves every completion it returns, to replay later."""

    def __init__(self, inner: Provider, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.name = inner.name
        self.written: list[Path] = []

    def complete(self, request: Request) -> Completion:
        completion = self.inner.complete(request)
        self.written.append(write_recording(self.directory, request, completion, self.inner.name))
        return completion
