import json
from pathlib import Path

import pytest

from codelens.providers import (
    Completion,
    ProviderError,
    RecordedProvider,
    Recorder,
    RecordingMissing,
    Request,
    Usage,
)
from codelens.providers.recorded import HAND_WRITTEN, load_recording, write_recording

REQUEST = Request("sys", "prompt", {"type": "object"})


def test_key_is_sha256_of_canonical_json() -> None:
    # Pinned: if the encoding ever changed, every saved recording would stop matching at once.
    # printf '%s' '{"prompt":"prompt","schema":null,"system":"sys"}' | shasum -a 256
    pinned = "cbe96d4abebc97426290af5a5f709c49efe6f0e81c74aa31ab3c9bb62d2044fb"
    assert Request("sys", "prompt").key() == pinned


def test_key_covers_system_prompt_and_schema_but_not_key_order() -> None:
    keys = {
        REQUEST.key(),
        Request("sys2", "prompt", {"type": "object"}).key(),
        Request("sys", "prompt2", {"type": "object"}).key(),
        Request("sys", "prompt", {"type": "array"}).key(),
        Request("sys", "prompt").key(),
    }
    assert len(keys) == 5
    a = Request("s", "p", {"type": "object", "required": []})
    b = Request("s", "p", {"required": [], "type": "object"})
    assert a.key() == b.key()


def test_replays_a_written_recording(tmp_path: Path) -> None:
    completion = Completion('{"findings": []}', HAND_WRITTEN, Usage(120, 7))
    path = write_recording(tmp_path, REQUEST, completion, "recorded")
    assert path.name == f"{REQUEST.key()}.json"
    assert RecordedProvider(tmp_path).complete(REQUEST) == completion
    stored = json.loads(path.read_text())
    assert stored["prompt"] == "prompt" and stored["model"] == HAND_WRITTEN


def test_a_miss_fails_loudly_and_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(RecordingMissing, match=REQUEST.key()):
        RecordedProvider(tmp_path).complete(REQUEST)


def test_an_edited_recording_is_refused(tmp_path: Path) -> None:
    path = write_recording(tmp_path, REQUEST, Completion("{}", HAND_WRITTEN), "recorded")
    record = json.loads(path.read_text())
    record["prompt"] = "a different prompt"
    path.write_text(json.dumps(record))
    with pytest.raises(ProviderError, match="does not match its key"):
        RecordedProvider(tmp_path).complete(REQUEST)


@pytest.mark.parametrize("content", ["not json", "{}", '{"system": "s", "prompt": "p"}'])
def test_unreadable_recordings_raise(tmp_path: Path, content: str) -> None:
    path = tmp_path / f"{REQUEST.key()}.json"
    path.write_text(content)
    with pytest.raises(ProviderError, match="unreadable recording"):
        load_recording(path)


class Echo:
    name = "echo"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: Request) -> Completion:
        self.calls += 1
        return Completion(f"echo: {request.prompt}", "echo-1", Usage(3, 2))


def test_recorder_saves_what_the_live_provider_said(tmp_path: Path) -> None:
    live = Echo()
    recorder = Recorder(live, tmp_path)
    first = recorder.complete(REQUEST)
    assert first.text == "echo: prompt" and live.calls == 1
    assert recorder.written == [tmp_path / f"{REQUEST.key()}.json"]
    assert json.loads(recorder.written[0].read_text())["provider"] == "echo"
    assert RecordedProvider(tmp_path).complete(REQUEST) == first
