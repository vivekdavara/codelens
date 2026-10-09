"""Seeded fuzzing of every parser that reads untrusted input: model answers, vendor responses, error bodies
and recordings. Each must either return a well-formed result or raise its own documented error, never a
stray TypeError or KeyError, whatever JSON it is handed.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from codelens.diff import parse_patch
from codelens.findings import (
    MAX_BODY,
    MAX_TITLE,
    FindingsFormatError,
    anchor_finding,
    check_response,
    parse_findings,
)
from codelens.providers import ProviderError
from codelens.providers.anthropic import parse_message
from codelens.providers.http import _error_details
from codelens.providers.openai import parse_chat_completion
from codelens.providers.recorded import load_recording

RUNS = 1500
DIFF = """\
--- a/svc/pay.py
+++ b/svc/pay.py
@@ -1,2 +1,5 @@
 def refund(amount, fee):
-    return amount - fee
+    net = amount - fee
+    if net < 0:
+        net = 0
+    return net
"""
PATCH = parse_patch(DIFF)
STRINGS = ["", " ", "svc/pay.py", "high", "if net < 0:", "net = 0", "é\u2028", "x" * 5000, "0", "\x00"]
NUMBERS: list[Any] = [0, 1, -1, 3, 5, 0.5, 1.0, 1.5, -0.0, 10**30, 1e308, True, False]


def random_json(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.choice(["null", "bool", "num", "str", "list", "dict"] if depth < 3 else ["null", "num", "str"])
    if kind == "null":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "num":
        return rng.choice(NUMBERS)
    if kind == "str":
        return rng.choice(STRINGS)
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["path", "line", "quote", "severity", "category", "title", "body", "confidence", "type", "content"]
    return {rng.choice(keys): random_json(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def valid_item(rng: random.Random) -> dict[str, Any]:
    line, quote = rng.choice(
        [(2, "net = amount - fee"), (3, "if net < 0:"), (4, "net = 0"), (5, "return net")]
    )
    return {
        "path": "svc/pay.py",
        "line": line,
        "quote": quote,
        "severity": rng.choice(["critical", "high", "medium", "low"]),
        "category": rng.choice(["bug", "security", "performance", "maintainability", "test"]),
        "title": "Title",
        "body": "Body",
        "confidence": rng.random(),
    }


def mutate(rng: random.Random, value: Any) -> Any:
    """Replace, drop or add one thing somewhere inside ``value``."""
    if isinstance(value, dict) and value and rng.random() < 0.7:
        key = rng.choice(list(value))
        op = rng.choice(["replace", "drop", "add", "recurse"])
        out = dict(value)
        if op == "replace":
            out[key] = random_json(rng)
        elif op == "drop":
            del out[key]
        elif op == "add":
            out[rng.choice(["extra", "Line", key + "_"])] = random_json(rng)
        else:
            out[key] = mutate(rng, out[key])
        return out
    if isinstance(value, list) and value and rng.random() < 0.7:
        out_list = list(value)
        i = rng.randrange(len(out_list))
        out_list[i] = mutate(rng, out_list[i])
        return out_list
    return random_json(rng)


def answers(rng: random.Random) -> list[str]:
    """Model answers: valid ones, mutated ones, random JSON, and some broken text."""
    items = [valid_item(rng) for _ in range(rng.randint(0, 4))]
    candidates: list[Any] = [
        {"findings": items},
        {"findings": [mutate(rng, item) for item in items]},
        mutate(rng, {"findings": items}),
        random_json(rng),
    ]
    texts = [json.dumps(c) for c in candidates]
    texts.append(texts[0][: rng.randrange(len(texts[0]) + 1)])  # truncated mid-JSON
    return texts


def test_findings_parsing_never_raises_anything_else() -> None:
    rng = random.Random(1)
    parsed = rejected = 0
    for _ in range(RUNS):
        for text in answers(rng):
            try:
                findings, rejections = parse_findings(text)
            except FindingsFormatError:
                continue
            parsed += len(findings)
            rejected += len(rejections)
            assert len(findings) + len(rejections) == len(json.loads(text)["findings"])
            for f in findings:
                assert type(f.line) is int and f.line >= 1
                assert 0 <= f.confidence <= 1
                assert f.title and "\n" not in f.title and len(f.title) <= MAX_TITLE
                assert f.body and len(f.body) <= MAX_BODY
    assert parsed > 1000 and rejected > 1000  # the generator really exercises both outcomes


def test_kept_findings_always_anchor_on_the_quoted_line() -> None:
    rng = random.Random(2)
    kept = 0
    for _ in range(RUNS):
        for text in answers(rng):
            try:
                findings, rejections = check_response(text, PATCH)
            except FindingsFormatError:
                continue
            # Kept findings carry their line's full text, so check the quote the model actually sent.
            rejected = {r.index for r in rejections}
            items = [item for i, item in enumerate(json.loads(text)["findings"]) if i not in rejected]
            assert len(items) == len(findings)
            for item, f in zip(items, findings, strict=True):
                assert anchor_finding(f, PATCH) is None
                line = PATCH.files[0].anchor(f.line)
                assert line is not None and f.quote == line.content
                sent, actual = " ".join(item["quote"].split()), " ".join(line.content.split())
                assert sent == actual or (sent and sent in actual)
                kept += 1
    assert kept > 500


def vendor_message(rng: random.Random) -> dict[str, Any]:
    return {
        "type": "message",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": '{"findings": []}'}],
        "stop_reason": rng.choice(["end_turn", "max_tokens", "refusal", None]),
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def chat_completion(rng: random.Random) -> dict[str, Any]:
    return {
        "model": "gpt-5",
        "choices": [
            {"message": {"content": "{}", "refusal": None}, "finish_reason": rng.choice(["stop", "length"])}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.parametrize(
    ("parse", "valid"), [(parse_message, vendor_message), (parse_chat_completion, chat_completion)]
)
def test_vendor_responses_only_raise_provider_errors(parse: Any, valid: Any) -> None:
    rng = random.Random(3)
    ok = failed = 0
    for _ in range(RUNS):
        for data in (mutate(rng, valid(rng)), mutate(rng, mutate(rng, valid(rng))), random_json(rng)):
            try:
                completion = parse(data)
            except ProviderError:
                failed += 1
                continue
            assert isinstance(completion.text, str) and completion.text
            assert isinstance(completion.model, str)
            assert completion.usage.input_tokens >= 0 and completion.usage.output_tokens >= 0
            ok += 1
    assert ok > 100 and failed > 100


def test_error_bodies_never_raise() -> None:
    rng = random.Random(4)
    for _ in range(RUNS):
        body = (
            json.dumps(random_json(rng)).encode()
            if rng.random() < 0.8
            else bytes(rng.randrange(256) for _ in range(20))
        )
        message, kind = _error_details(body)
        assert isinstance(message, str) and (kind is None or isinstance(kind, str))


def test_recordings_with_any_content_only_raise_provider_errors(tmp_path: Path) -> None:
    rng = random.Random(5)
    path = tmp_path / "0123.json"
    base = {
        "key": "0123",
        "system": "s",
        "prompt": "p",
        "schema": None,
        "response": "r",
        "model": "m",
        "usage": {},
    }
    for _ in range(300):
        path.write_text(json.dumps(mutate(rng, base)))
        with pytest.raises(ProviderError):  # never valid: no request hashes to "0123"
            load_recording(path)
