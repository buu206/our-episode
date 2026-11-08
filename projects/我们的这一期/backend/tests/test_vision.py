"""Real local frame extraction; all supplier traffic is httpx.MockTransport."""

import asyncio
import base64
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
import subprocess
from uuid import uuid4

import httpx
from PIL import Image
import pytest

from app.services import media, vision
from app.services.vision import (CloudVision, FrameSample, PreparedVideo, VisionError,
                                 parse_observations, prepare_video)

CONFIG = {"MODEL_API_KEY": "test-only-key", "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
          "MODEL_NAME": "qwen3-vl-plus", "CLOUD_ENABLED": True,
          "CLOUD_RETENTION_VERIFIED": True, "CLOUD_REGION_VERIFIED": True}


@pytest.fixture
def local_video(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    path = root / (uuid4().hex + ".mp4")
    subprocess.run([media._binary("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=64x32:r=2:d=31", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-threads", "1", str(path)], check=True, timeout=20, capture_output=True)
    return root, {"asset_id": uuid4().hex, "path": path}


@pytest.fixture
def prepared(local_video):
    root, asset = local_video
    return prepare_video([asset], media_root=root)


def result_for(frames):
    return {"observations": [{"frame_id": f.frame_id, "asset_id": f.asset_id,
                              "time": f.time, "note": "画面呈红色。"} for f in frames],
            "uncertainties": ["仅有静态采样。"]}


def response_for(frames, *, usage=None, content=None, finish="stop"):
    return httpx.Response(200, json={"id": "vision-request-1",
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(result_for(frames), ensure_ascii=False)
                     if content is None else content}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100} if usage is None else usage})


def adapter(handler, *, config=None, budget_check=None, client_factory=None):
    return CloudVision(CONFIG if config is None else config,
        client_factory=client_factory or (lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        budget_check=budget_check or (lambda reservation, amount, digest: reservation == "reserved"))


def analyze(service, prepared):
    return asyncio.run(service.analyze(prepared, reservation="reserved"))


def test_real_ffmpeg_sampling_preserves_source_and_records_all_gaps(local_video):
    root, asset = local_video
    before = hashlib.sha256(asset["path"].read_bytes()).hexdigest()
    packet = prepare_video([asset], media_root=root)
    assert len(packet.frames) == 3
    coverage = packet.coverage[0]
    assert coverage.duration == pytest.approx(31)
    assert coverage.sample_times == pytest.approx([31 / 6, 31 / 2, 31 * 5 / 6], abs=1e-6)
    assert coverage.audio_analyzed is False
    assert coverage.time_basis == "nominal_seek_seconds"
    assert len(coverage.unsampled_intervals) == 4
    assert sum(g.end - g.start for g in coverage.unsampled_intervals) == pytest.approx(31)
    assert coverage.max_gap_seconds <= 15
    for frame in packet.frames:
        with Image.open(io.BytesIO(frame.jpeg)) as image:
            assert image.size == (512, 512)
            assert image.getpixel((256, 256))[0] > 230
            assert max(image.getpixel((1, 1))) < 10  # black pad; no crop
    assert before == hashlib.sha256(asset["path"].read_bytes()).hexdigest()
    assert list(root.iterdir()) == [asset["path"]]  # no persisted JPEG/Base64
    assert "jpeg" not in repr(packet)
    assert str(asset["path"]) not in json.dumps(packet.manifest())
    assert len(packet.request_digest) == 64
    assert packet.required_reservation_yuan == 0.85


@pytest.mark.parametrize("bad", ["https://example.invalid/video.mp4", Path("relative.mp4"), None])
def test_reject_nonlocal_or_relative_input_before_processing(local_video, bad):
    root, asset = local_video
    with pytest.raises(VisionError) as error:
        prepare_video([{**asset, "path": bad}], media_root=root)
    assert error.value.code == "INVALID_LOCAL_ASSET"


def test_reject_non_uuid_name_symlink_and_file_outside_root(local_video, tmp_path):
    root, asset = local_video
    named = root / "someone-secret-name.mp4"
    named.write_bytes(asset["path"].read_bytes())
    link = root / (uuid4().hex + ".mp4")
    link.symlink_to(asset["path"])
    outside = tmp_path / (uuid4().hex + ".mp4")
    outside.write_bytes(asset["path"].read_bytes())
    for path in (named, link, outside):
        with pytest.raises(VisionError):
            prepare_video([{**asset, "path": path}], media_root=root)


def test_malformed_media_rejected_by_real_decoder(local_video):
    root, asset = local_video
    asset["path"].write_bytes(b"not a video")
    with pytest.raises(media.MediaError):
        prepare_video([asset], media_root=root)


def test_limits_and_duplicate_assets_prevent_extracting(local_video, monkeypatch):
    root, asset = local_video
    with pytest.raises(VisionError):
        prepare_video([asset] * 9, media_root=root)
    with pytest.raises(VisionError):
        prepare_video([asset, asset], media_root=root)
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 601.0})
    monkeypatch.setattr(vision, "_jpeg_frame", lambda *args: pytest.fail("must not extract over limit"))
    with pytest.raises(VisionError) as error:
        prepare_video([asset], media_root=root)
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"


def test_ffmpeg_arguments_forbid_network_and_use_pipe(local_video, monkeypatch):
    root, asset = local_video
    original = media._run
    seen = []

    def capture(name, args, **kwargs):
        seen.append((name, args, kwargs))
        return original(name, args, **kwargs)

    monkeypatch.setattr(media, "_run", capture)
    prepare_video([asset], media_root=root)
    commands = [args for name, args, _ in seen if name == "ffmpeg" and "image2pipe" in args]
    assert len(commands) == 3
    for args in commands:
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert args[args.index("-enable_drefs") + 1] == "0"
        assert args[args.index("-format_whitelist") + 1] == "mov"
        assert args[-1] == "pipe:1"


def test_mock_request_sends_real_image_content_and_unverified_observations(prepared, local_video):
    requests, checks = [], []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "qwen3-vl-plus"
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 2048
        assert str(local_video[1]["path"]).encode() not in request.content
        images = [entry["image_url"]["url"] for entry in body["messages"][0]["content"] if entry["type"] == "image_url"]
        assert len(images) == len(prepared.frames)
        assert base64.b64decode(images[0].split(",")[1]) == prepared.frames[0].jpeg
        return response_for(prepared.frames)

    def permit(reservation, amount, digest):
        checks.append((reservation, amount, digest))
        return reservation == "reserved" and amount == 0.85 and digest == prepared.request_digest

    result = analyze(adapter(handler, budget_check=permit), prepared)
    assert len(requests) == 1
    assert len(checks) >= 1
    assert result["result"].verification == "unverified"
    assert all(item.verification == "unverified" for item in result["result"].observations)
    assert result["result"].coverage == list(prepared.coverage)
    assert any("音轨" in item for item in result["result"].uncertainties)
    assert result["usage"]["cost_status"] == "known"
    assert result["usage"]["cost_yuan"] == pytest.approx(0.002)


@pytest.mark.parametrize("change", [
    {"CLOUD_ENABLED": False}, {"CLOUD_ENABLED": "true"}, {"CLOUD_RETENTION_VERIFIED": False},
    {"CLOUD_REGION_VERIFIED": 1}, {"MODEL_API_KEY": ""},
    {"MODEL_BASE_URL": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    {"MODEL_NAME": "different-model"},
])
def test_cloud_guards_run_before_client_initialization(prepared, change):
    service = adapter(lambda _: pytest.fail("no request"), config={**CONFIG, **change},
                      client_factory=lambda: pytest.fail("no client"))
    with pytest.raises(VisionError):
        analyze(service, prepared)


@pytest.mark.parametrize("answer", [False, 1, None])
def test_budget_requires_actual_true_and_packet_binding(prepared, answer):
    service = adapter(lambda _: pytest.fail("no request"), budget_check=lambda *args: answer,
                      client_factory=lambda: pytest.fail("no client"))
    with pytest.raises(VisionError) as error:
        analyze(service, prepared)
    assert error.value.code == "BUDGET_PERMISSION_REQUIRED"
    assert error.value.usage["cost_status"] == "not_sent"


@pytest.mark.parametrize("change", [
    {"frame_id": "invented-frame"}, {"asset_id": "unowned-asset"}, {"time": 600.0},
    {"time": "0"}, {"time": float("nan")}, {"note": " "}, {"verification": "verified"},
    {"dialogue": "invented quotation"},
])
def test_model_cannot_change_source_time_or_promote_verification(prepared, change):
    data = result_for(prepared.frames)
    data["observations"][0].update(change)
    with pytest.raises(VisionError) as error:
        parse_observations(json.dumps(data), prepared.frames)
    assert error.value.code == "INVALID_MODEL_OUTPUT"


@pytest.mark.parametrize("content", ["not json", "[]", '{"observations":[],"uncertainties":[]}',
    '{"observations":[],"uncertainties":["unknown"],"uncertainties":[]}'])
def test_invalid_model_json_is_rejected(prepared, content):
    with pytest.raises(VisionError):
        parse_observations(content, prepared.frames)


def test_prepared_packet_cannot_hide_unseen_frames(prepared):
    tampered = replace(prepared, frames=(replace(prepared.frames[0], time=999.0), *prepared.frames[1:]))
    with pytest.raises(VisionError) as error:
        analyze(adapter(lambda _: pytest.fail("must not send altered packet")), tampered)
    assert error.value.code == "INVALID_PREPARED_VIDEO"


def multi_group(jpeg, assets=3):
    coverages = tuple(vision._coverage(uuid4().hex, 200.0) for _ in range(assets))
    frames = tuple(FrameSample(c.asset_id, t, f"{c.asset_id}_f{i + 1:03d}", jpeg)
                   for c in coverages for i, t in enumerate(c.sample_times))
    return PreparedVideo(frames, coverages)


def test_three_groups_reserve_under_task_cap_and_aggregate_all_known_usage(prepared):
    packet = multi_group(prepared.frames[0].jpeg)
    assert len(packet.frames) == 42
    assert packet.required_reservation_yuan == 2.55 < 3
    calls, permissions = [], []

    def handler(request):
        frames = packet.frames[len(calls) * 16:(len(calls) + 1) * 16]
        calls.append(request)
        return response_for(frames)

    async def permit(reservation, amount, digest):
        permissions.append((amount, digest))
        return amount == 2.55 and digest == packet.request_digest

    result = analyze(adapter(handler, budget_check=permit), packet)
    assert len(calls) == 3
    assert len(permissions) >= 3
    assert len(result["result"].observations) == 42
    assert result["usage"]["input_tokens"] == 3000
    assert result["usage"]["cost_yuan"] == pytest.approx(0.006)


@pytest.mark.parametrize("failure", ["timeout", "http", "missing_usage", "truncated"])
def test_failures_never_auto_retry_and_preserve_unknown_charge(prepared, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-provider-content", request=request)
        if failure == "http":
            return httpx.Response(500, text="private-provider-content")
        if failure == "missing_usage":
            return response_for(prepared.frames, usage={})
        return response_for(prepared.frames, finish="length")

    if failure == "missing_usage":
        result = analyze(adapter(handler), prepared)
        assert result["usage"]["cost_status"] == "unknown"
        assert result["usage"]["cost_yuan"] is None
    else:
        with pytest.raises(VisionError) as error:
            analyze(adapter(handler), prepared)
        assert "private-provider-content" not in str(error.value)
        if failure != "truncated":
            assert error.value.usage["cost_status"] == "unknown"
    assert len(calls) == 1


def test_later_group_timeout_retains_earlier_usage_and_whole_reservation(prepared):
    packet = multi_group(prepared.frames[0].jpeg)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return response_for(packet.frames[:16])
        raise httpx.ReadTimeout("private error", request=request)

    with pytest.raises(VisionError) as error:
        analyze(adapter(handler), packet)
    usage = error.value.usage
    assert len(calls) == 2
    assert usage["cost_status"] == "unknown" and usage["cost_yuan"] is None
    assert usage["requests"][0]["cost_status"] == "known"
    assert usage["requests"][1]["cost_status"] == "unknown"


def test_client_initialization_failure_has_no_charge(prepared):
    def fail():
        raise RuntimeError("private client config")
    with pytest.raises(VisionError) as error:
        analyze(adapter(lambda _: pytest.fail("no request"), client_factory=fail), prepared)
    assert error.value.code == "CLIENT_INIT_FAILED"
    assert error.value.usage["cost_status"] == "not_sent"
    assert "private client config" not in str(error.value)
