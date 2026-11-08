"""Offline speech adapter tests; synthetic local audio and MockTransport only."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import wave

import httpx
import pytest

from app.services.speech import (ASR_MODEL, ENDPOINT, TTS_AUDIO_HOST, TTS_MODEL, AudioChunk,
                                 CloudSpeech, SpeechError, asr_reservation_yuan,
                                 billable_characters, extract_audio_chunks, parse_transcription,
                                 tts_reservation_yuan)


CONFIG = {"MODEL_API_KEY": "test-key", "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
          "CLOUD_ENABLED": True, "CLOUD_RETENTION_VERIFIED": True, "CLOUD_REGION_VERIFIED": True}


def wav_data(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(seconds * rate))
    return buffer.getvalue()


@pytest.fixture
def chunk(tmp_path):
    path = tmp_path / "local.wav"
    path.write_bytes(wav_data())
    return AudioChunk(path, "asset-1", 30.0, 1.0)


def asr_response():
    return {"request_id": "speech-1", "usage": {"duration": 1}, "output": {
        "text": "你好。", "sentence": {"sentence_id": 1, "sentence_end": True,
            "begin_time": 100, "end_time": 900, "text": "你好。", "channel_id": 0,
            "words": [{"begin_time": 100, "end_time": 400, "fixed": True, "text": "你", "punctuation": ""},
                      {"begin_time": 400, "end_time": 900, "fixed": True, "text": "好", "punctuation": "。"}]}}}


def tts_response(*, url=None, data=None):
    return {"request_id": "tts-1", "usage": {"characters": 4}, "output": {
        "finish_reason": "stop", "audio": {"data": "" if data is None else data,
            "url": url or "https://" + TTS_AUDIO_HOST + "/test.wav?Expires=123"}}}


def service(handler, budget=lambda _: True):
    return CloudSpeech(CONFIG, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                       budget_check=budget)


def transcribe(adapter, chunk):
    return asyncio.run(adapter.transcribe([chunk], reservation="reserve-1"))


def synthesize(adapter, path):
    return asyncio.run(adapter.synthesize("你好", path, reservation="reserve-1"))


@pytest.mark.parametrize("change", [{"CLOUD_ENABLED": False}, {"CLOUD_ENABLED": "true"},
                                    {"CLOUD_RETENTION_VERIFIED": False}, {"CLOUD_REGION_VERIFIED": 1},
                                    {"MODEL_API_KEY": ""}, {"MODEL_BASE_URL": None},
                                    {"MODEL_BASE_URL": "https://dashscope-intl.aliyuncs.com/api/v1"},
                                    {"ASR_MODEL": "fun-asr"}, {"TTS_MODEL": "other"}])
def test_cloud_guard_before_client_creation(chunk, tmp_path, change):
    adapter = CloudSpeech({**CONFIG, **change}, client_factory=lambda: pytest.fail("client constructed"),
                          budget_check=lambda _: True)
    assert adapter.ready()[0] is False
    with pytest.raises(SpeechError):
        transcribe(adapter, chunk)
    with pytest.raises(SpeechError):
        synthesize(adapter, tmp_path / "out.wav")


def test_key_alone_cannot_unlock(chunk):
    adapter = CloudSpeech({"MODEL_API_KEY": "test-key"}, client_factory=lambda: pytest.fail("created"))
    with pytest.raises(SpeechError) as error:
        transcribe(adapter, chunk)
    assert error.value.code == "CLOUD_DISABLED"


@pytest.mark.parametrize("permission", [None, lambda _: False, lambda _: 1])
def test_budget_guard_before_client_creation(chunk, permission):
    adapter = CloudSpeech(CONFIG, client_factory=lambda: pytest.fail("created"), budget_check=permission)
    with pytest.raises(SpeechError) as error:
        transcribe(adapter, chunk)
    assert error.value.code == "BUDGET_PERMISSION_REQUIRED"
    assert error.value.usage["cost_status"] == "not_sent"


def test_client_initialisation_failure_is_sanitised(chunk):
    def fail():
        raise RuntimeError("test-key provider details")
    adapter = CloudSpeech(CONFIG, client_factory=fail, budget_check=lambda _: True)
    with pytest.raises(SpeechError) as error:
        transcribe(adapter, chunk)
    assert error.value.code == "CLIENT_INIT_FAILED"
    assert "test-key" not in str(error.value)


def test_asr_sends_only_base64_wav_and_returns_absolute_word_times(chunk):
    def respond(request):
        assert str(request.url) == ENDPOINT
        body = json.loads(request.content)
        assert body["model"] == ASR_MODEL
        assert body["parameters"] == {"format": "wav", "sample_rate": "16000"}
        value = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
        assert value.startswith("data:audio/wav;base64,")
        assert base64.b64decode(value.split(",", 1)[1]) == chunk.path.read_bytes()
        return httpx.Response(200, json=asr_response())
    result = transcribe(service(respond), chunk)
    assert result["result"] == [{"asset_id": "asset-1", "start": 30.1, "end": 30.4, "text": "你"},
                                {"asset_id": "asset-1", "start": 30.4, "end": 30.9, "text": "好。"}]
    assert result["usage"]["cost_yuan"] == 0.00022
    assert result["usage"]["input_tokens"] is None
    assert result["usage"]["seconds"] == 1


@pytest.mark.parametrize("change", [{"begin_time": -1}, {"end_time": 1001}, {"begin_time": True},
                                    {"begin_time": "100"}, {"begin_time": 500}, {"fixed": False}])
def test_asr_rejects_unstable_or_invalid_word_times(chunk, change):
    envelope = asr_response()
    envelope["output"]["sentence"]["words"][0].update(change)
    with pytest.raises(SpeechError):
        parse_transcription(envelope, chunk)


def test_asr_refuses_missing_sentence_timestamps_in_full_text(chunk):
    envelope = asr_response()
    envelope["output"]["text"] = "前一句。你好。"
    with pytest.raises(SpeechError) as error:
        transcribe(service(lambda _: httpx.Response(200, json=envelope)), chunk)
    assert error.value.code == "INCOMPLETE_TIMESTAMPS"
    assert error.value.usage["cost_yuan"] == 0.00022


def test_silence_is_empty_only_when_explicitly_returned(chunk):
    assert parse_transcription({"output": {"text": "", "sentence": None}}, chunk) == []
    with pytest.raises(SpeechError):
        parse_transcription({"output": {}}, chunk)


@pytest.mark.parametrize("update", [{"duration": 2.0}, {"offset": -1}, {"offset": float("nan")},
                                    {"offset": 10 ** 400}, {"duration": True}])
def test_local_audio_metadata_is_verified_before_network(chunk, update):
    adapter = service(lambda _: pytest.fail("outbound request"))
    with pytest.raises(SpeechError):
        transcribe(adapter, replace(chunk, **update))


def test_budget_counts_rounding_and_chinese_double_characters(chunk):
    assert asr_reservation_yuan([chunk]) == 0.00044
    assert billable_characters("中A文123") == 8
    assert billable_characters("中 文。") == 6
    assert billable_characters("かな한글") == 4
    assert tts_reservation_yuan("中" * 600) == 0.192
    with pytest.raises(SpeechError):
        tts_reservation_yuan("中" * 601)


def test_asr_timeout_unknown_without_retry(chunk):
    attempts = []
    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("test-key", request=request)
    with pytest.raises(SpeechError) as error:
        transcribe(service(timeout), chunk)
    assert len(attempts) == 1
    assert error.value.usage["cost_yuan"] is None
    assert "test-key" not in str(error.value)


def test_retry_success_keeps_uncertain_attempt_cost_reserved(chunk):
    attempts = []
    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=asr_response())
    result = transcribe(service(respond), chunk)
    assert len(attempts) == 2
    assert result["usage"]["cost_yuan"] is None
    assert result["usage"]["retries"] == 1


def test_two_failed_connections_are_the_maximum(chunk):
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ConnectError("offline", request=request)
    with pytest.raises(SpeechError) as error:
        transcribe(service(fail), chunk)
    assert len(calls) == 2 and error.value.usage["cost_yuan"] is None


def test_failed_later_chunk_keeps_prior_billed_requests(chunk):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=asr_response())
        raise httpx.ReadTimeout("offline", request=request)
    with pytest.raises(SpeechError) as error:
        asyncio.run(service(respond).transcribe([chunk, replace(chunk, offset=31)], reservation="reserved"))
    assert error.value.usage["cost_yuan"] is None
    assert error.value.usage["requests"][0]["cost_yuan"] == 0.00022


def test_missing_usage_cannot_be_assumed_free(chunk):
    envelope = asr_response()
    envelope.pop("usage")
    result = transcribe(service(lambda _: httpx.Response(200, json=envelope)), chunk)
    assert result["usage"]["cost_yuan"] is None


def test_tts_pcm_base64_writes_valid_wav_with_no_download(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == TTS_MODEL and body["input"]["voice"] == "Cherry"
        return httpx.Response(200, json=tts_response(data=base64.b64encode(b"\0\0" * 24000).decode()))
    target = tmp_path / "voice.wav"
    result = synthesize(service(respond), target)
    assert result["result"] == {"path": str(target), "duration": 1.0}
    assert result["usage"]["characters"] == 4
    assert result["usage"]["cost_yuan"] == 0.00032
    with wave.open(str(target)) as audio:
        assert audio.getframerate() == 24000 and audio.getnframes() == 24000
    assert len(requests) == 1


def test_tts_download_is_https_without_auth_or_redirect(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=tts_response(url="http://" + TTS_AUDIO_HOST + "/voice.wav?Expires=123"))
        assert request.url.scheme == "https" and request.url.host == TTS_AUDIO_HOST
        assert "authorization" not in request.headers
        return httpx.Response(200, content=wav_data(rate=24000))
    result = synthesize(service(respond), tmp_path / "voice.wav")
    assert result["result"]["duration"] == 1 and len(calls) == 2


@pytest.mark.parametrize("url", ["https://127.0.0.1/a.wav", "https://example.com/a.wav",
    "https://" + TTS_AUDIO_HOST + ".evil.test/a.wav", "https://user@" + TTS_AUDIO_HOST + "/a.wav",
    "https://" + TTS_AUDIO_HOST + ":444/a.wav", "file:///tmp/voice.wav"])
def test_tts_rejects_other_download_hosts_after_recording_generation_cost(tmp_path, url):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=tts_response(url=url))
    target = tmp_path / "voice.wav"
    with pytest.raises(SpeechError) as error:
        synthesize(service(respond), target)
    assert error.value.code == "AUDIO_URL_NOT_ALLOWED"
    assert error.value.usage["cost_yuan"] == 0.00032
    assert len(calls) == 1 and not target.exists()


def test_download_redirect_is_not_followed_and_cost_stays_known(tmp_path):
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=tts_response())
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    with pytest.raises(SpeechError) as error:
        synthesize(service(respond), tmp_path / "voice.wav")
    assert len(calls) == 2 and error.value.usage["cost_yuan"] == 0.00032


@pytest.mark.parametrize("data", [b"bad file", wav_data(31, 24000), wav_data(1, 24000)[:-10]])
def test_tts_invalid_or_over_30_second_download_is_not_published(tmp_path, data):
    def respond(request):
        return httpx.Response(200, json=tts_response()) if request.method == "POST" else httpx.Response(200, content=data)
    target = tmp_path / "voice.wav"
    with pytest.raises(SpeechError) as error:
        synthesize(service(respond), target)
    assert error.value.code == "INVALID_AUDIO"
    assert error.value.usage["cost_yuan"] == 0.00032
    assert not target.exists()


def test_tts_never_overwrites_existing_file(tmp_path):
    target = tmp_path / "voice.wav"
    target.write_bytes(b"existing")
    with pytest.raises(SpeechError):
        synthesize(service(lambda _: pytest.fail("called model")), target)
    assert target.read_bytes() == b"existing"


def test_tts_failed_disk_sync_removes_only_its_incomplete_output(tmp_path, monkeypatch):
    target = tmp_path / "voice.wav"
    existing = tmp_path / "keep.wav"
    existing.write_bytes(b"previous output")
    envelope = tts_response(data=base64.b64encode(b"\0\0" * 24000).decode())
    def fail_sync(_):
        raise OSError("simulated disk failure")
    monkeypatch.setattr("app.services.speech.os.fsync", fail_sync)
    with pytest.raises(SpeechError) as error:
        synthesize(service(lambda _: httpx.Response(200, json=envelope)), target)
    assert error.value.code == "TTS_PROCESSING_FAILED"
    assert error.value.usage["cost_yuan"] == 0.00032
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b"previous output"


def test_real_local_extraction_splits_exact_samples_without_changing_source(tmp_path):
    executable = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(executable).is_file():
        pytest.skip("FFmpeg unavailable")
    source = tmp_path / "input ; literal.mp4"
    subprocess.run([executable, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.5",
                    "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                   check=True, capture_output=True, timeout=20)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    chunks = extract_audio_chunks(source, tmp_path / "chunks", "asset-real", chunk_seconds=1)
    assert len(chunks) == 2
    assert chunks[0].offset == 0 and chunks[0].duration == 1
    assert chunks[1].offset == 1 and chunks[1].duration == pytest.approx(0.5, abs=0.02)
    assert asr_reservation_yuan(chunks) == 0.00088
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
