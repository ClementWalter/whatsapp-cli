"""Voice-note transcription in `wa read`: which messages count as audio, the per-message cache, and the CLI output."""

from __future__ import annotations

import json
import subprocess

import pytest
from click.testing import CliRunner

from wa.cache import CachedMessage
from wa.transcripts import is_audio, load_transcripts, transcribe_messages

VOICE_NOTE = {"kind": "audio", "mimetype": "audio/ogg; codecs=opus", "seconds": 7, "ptt": 1}


def note(msg_id: str = "3A0E8ACA6B24EA044A13", media: dict | None = VOICE_NOTE) -> CachedMessage:
    return CachedMessage(ts=1795400000, chat="10000000000000@lid", sender="10000000000000@lid",
                         sender_name="Alice", text="[audio]", from_me=False, msg_id=msg_id, media=media)


def stub_run(text: str = "on se voit demain", returncode: int = 0):
    """Stands in for `transcribe file --json`, recording each command it is given."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        out = json.dumps({"text": f" {text} ", "language": "fr", "duration": 7.0, "backend": "whisper"})
        return subprocess.CompletedProcess(cmd, returncode, out if returncode == 0 else "", "boom")

    run.calls = calls
    return run


def stub_download(media: dict) -> bytes:
    return b"OggS"


@pytest.fixture
def store(tmp_path):
    return tmp_path / "transcripts.json"


def test_voice_note_is_audio():
    assert is_audio(VOICE_NOTE)


def test_audio_document_is_audio():
    assert is_audio({"kind": "document", "mimetype": "audio/mpeg"})


def test_image_is_not_audio():
    assert not is_audio({"kind": "image", "mimetype": "image/jpeg"})


def test_text_message_is_not_audio():
    assert not is_audio(None)


def test_transcript_text_is_stripped(store):
    got = transcribe_messages([note()], binary="transcribe", download=stub_download, run=stub_run(), path=store)
    assert got["3A0E8ACA6B24EA044A13"] == {"text": "on se voit demain", "language": "fr"}


def test_transcript_is_cached_on_disk(store):
    transcribe_messages([note()], binary="transcribe", download=stub_download, run=stub_run(), path=store)
    assert "3A0E8ACA6B24EA044A13" in load_transcripts(store)


def test_cached_note_is_not_transcribed_again(store):
    transcribe_messages([note()], binary="transcribe", download=stub_download, run=stub_run(), path=store)
    run = stub_run()
    transcribe_messages([note()], binary="transcribe", download=stub_download, run=run, path=store)
    assert run.calls == []


def test_runs_transcribe_file_with_json(store):
    run = stub_run()
    transcribe_messages([note()], binary="/opt/transcribe", download=stub_download, run=run, path=store)
    assert (run.calls[0][:2], run.calls[0][3]) == (["/opt/transcribe", "file"], "--json")


def test_audio_file_keeps_ogg_extension(store):
    run = stub_run()
    transcribe_messages([note()], binary="transcribe", download=stub_download, run=run, path=store)
    assert run.calls[0][2].endswith("3A0E8ACA6B24EA044A13.ogg")


def test_failed_transcription_is_left_out(store):
    got = transcribe_messages([note()], binary="transcribe", download=stub_download, run=stub_run(returncode=1), path=store)
    assert got == {}


def test_download_error_is_left_out(store):
    def broken(media: dict) -> bytes:
        raise RuntimeError("media url expired")

    got = transcribe_messages([note()], binary="transcribe", download=broken, run=stub_run(), path=store)
    assert got == {}


def test_without_binary_nothing_runs(store):
    run = stub_run()
    transcribe_messages([note()], binary=None, download=stub_download, run=run, path=store)
    assert run.calls == []


def test_without_binary_cached_transcripts_still_returned(store):
    transcribe_messages([note()], binary="transcribe", download=stub_download, run=stub_run(), path=store)
    got = transcribe_messages([note()], binary=None, download=stub_download, run=stub_run(), path=store)
    assert got["3A0E8ACA6B24EA044A13"]["text"] == "on se voit demain"


def test_text_messages_are_skipped(store):
    run = stub_run()
    transcribe_messages([note(media=None)], binary="transcribe", download=stub_download, run=run, path=store)
    assert run.calls == []


@pytest.fixture
def cli_with_note(monkeypatch, store):
    """`wa read` over a one-message cache whose voice note transcribes to a fixed text."""
    import wa.cache
    import wa.transcripts
    from wa.cli import cli

    monkeypatch.setattr(wa.cache, "find_chat", lambda q: [("10000000000000@lid", {"name": "Alice"})])
    monkeypatch.setattr(wa.cache, "iter_messages", lambda: iter([note()]))
    monkeypatch.setattr(wa.cache, "load_chats", lambda: {})
    monkeypatch.setattr(wa.cache, "load_contacts", lambda: {})
    monkeypatch.setattr(wa.cache, "load_lidmap", lambda: {})
    monkeypatch.setattr(wa.transcripts, "TRANSCRIPTS_PATH", store)
    monkeypatch.setattr(wa.transcripts, "transcribe_bin", lambda: "transcribe")
    monkeypatch.setattr("wa.media.download_media", stub_download)
    monkeypatch.setattr(wa.transcripts.subprocess, "run", stub_run())
    return cli


def test_read_json_carries_transcript(cli_with_note):
    result = CliRunner().invoke(cli_with_note, ["read", "alice", "--json", "--no-extend", "--limit", "1"])
    assert json.loads(result.stdout)["messages"][0]["transcript"]["text"] == "on se voit demain"


def test_read_text_appends_transcript(cli_with_note):
    result = CliRunner().invoke(cli_with_note, ["read", "alice", "--no-extend", "--limit", "1"])
    assert "[audio] «on se voit demain»" in result.stdout


def test_read_no_transcribe_leaves_transcript_null(cli_with_note):
    result = CliRunner().invoke(cli_with_note, ["read", "alice", "--json", "--no-extend", "--limit", "1", "--no-transcribe"])
    assert json.loads(result.stdout)["messages"][0]["transcript"] is None
