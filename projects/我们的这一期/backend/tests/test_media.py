from __future__ import annotations

import array
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest

from app.services import media
from app.services.media import MediaError, probe, render


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary and Path("/opt/homebrew/bin/ffmpeg").is_file():
        binary = "/opt/homebrew/bin/ffmpeg"
    if not binary:
        pytest.skip("Real FFmpeg integration requires the local media tool")
    return binary


def _make_video(path: Path, *, size="320x180", audio=True, seconds=1, rate=30, fmt="mp4") -> Path:
    args = [
        _ffmpeg(), "-nostdin", "-v", "error", "-y", "-threads", "2", "-filter_threads", "1",
        "-f", "lavfi", "-i", f"color=c=blue:size={size}:rate={rate}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", str(seconds), "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-ac", "2"]
    args += ["-f", fmt, str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=30, shell=False)
    return path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    return _make_video(tmp_path / "source.mp4")


def _audio_samples(path: Path, start: float, duration: float) -> array.array:
    result = subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-ss", str(start), "-i", str(path),
         "-t", str(duration), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        check=True, capture_output=True, timeout=20, shell=False,
    )
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


def test_probe_real_video_reports_container_and_decodable_stream(source: Path):
    info = probe(source)
    assert info["duration"] == pytest.approx(1, abs=0.04)
    assert info["width"] == 320 and info["height"] == 180
    assert info["has_audio"] is True
    assert info["format"] == "mp4"


def test_render_mixed_orientation_preserves_audio_and_adds_silence(source: Path, tmp_path: Path):
    silent = _make_video(tmp_path / "portrait.mov", size="180x320", audio=False, fmt="mov")
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, silent)}
    output = tmp_path / "result.mp4"
    result = render([
        {"path": source, "start": 0, "end": 0.8, "caption": "现场原声"},
        {"path": silent, "start": 0, "end": 0.8, "caption": "无声素材"},
    ], output, title="我们的这一期")
    assert (result["width"], result["height"]) == (1280, 720)
    assert result["duration"] == pytest.approx(1.6, abs=0.08)
    assert result["has_audio"]
    spoken = _audio_samples(output, 0.1, 0.2)
    padded = _audio_samples(output, 1.1, 0.2)
    assert spoken and max(abs(value) for value in spoken) > 100
    assert padded and max(abs(value) for value in padded) < 20
    frame = subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-ss", "1.1", "-i", str(output),
         "-frames:v", "1", "-vf", "scale=128:72", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        check=True, capture_output=True, timeout=20, shell=False,
    ).stdout
    # The portrait source fits inside the landscape canvas, retaining the full
    # frame and padding its sides rather than cropping it to landscape.
    left = frame[(36 * 128 + 5) * 3:(36 * 128 + 5) * 3 + 3]
    center = frame[(36 * 128 + 64) * 3:(36 * 128 + 64) * 3 + 3]
    assert len(frame) == 128 * 72 * 3
    assert max(left) < 20
    assert center[2] > 150 and center[0] < 30
    subprocess.run(
        [_ffmpeg(), "-nostdin", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
        check=True, capture_output=True, timeout=30, shell=False,
    )
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
    assert not list(tmp_path.glob(".render-*"))


def test_portrait_canvas_and_literal_special_text(tmp_path: Path):
    source = _make_video(tmp_path / "input 'quoted'.mov", size="180x320", audio=False, fmt="mov")
    marker = tmp_path / "must-not-exist"
    caption = f"引号 ' ; [v] % : ${{x}} $(touch '{marker}')\\n"
    output = tmp_path / "portrait.mp4"
    result = render([{"path": source, "start": 0, "end": 0.4, "caption": caption}], output)
    assert (result["width"], result["height"]) == (720, 1280)
    assert not marker.exists()
    assert output.is_file()


def test_bad_file_rejected_without_path_or_stderr(tmp_path: Path):
    fake = tmp_path / "private-party-name.mp4"
    fake.write_bytes(b"not a movie; https://example.invalid/private")
    with pytest.raises(MediaError) as error:
        probe(fake)
    assert error.value.code == "MEDIA_INVALID_FILE"
    assert str(tmp_path) not in error.value.message
    assert fake.name not in error.value.message
    assert "https" not in error.value.message


def test_wrong_container_is_not_accepted_from_mp4_extension(tmp_path: Path):
    disguised = _make_video(tmp_path / "looks-like.mp4", fmt="matroska")
    with pytest.raises(MediaError) as error:
        probe(disguised)
    assert error.value.code in {"MEDIA_INVALID_FILE", "MEDIA_UNSUPPORTED_CONTAINER"}


@pytest.mark.parametrize("start,end", [(-1, 0.5), (0.5, 0.5), (0, 2), (float("nan"), 1)])
def test_invalid_source_span_cannot_publish(source: Path, tmp_path: Path, start: float, end: float):
    output = tmp_path / "result.mp4"
    with pytest.raises(MediaError) as error:
        render([{"path": source, "start": start, "end": end, "caption": ""}], output)
    assert error.value.code == "MEDIA_INVALID_SEGMENT"
    assert not output.exists()
    assert not list(tmp_path.glob(".render-*"))


def test_total_duration_is_checked_before_rendering(tmp_path: Path):
    source = _make_video(tmp_path / "long.mp4", size="32x32", audio=False, seconds=31)
    output = tmp_path / "result.mp4"
    with pytest.raises(MediaError) as error:
        render([{"path": source, "start": 0, "end": 31, "caption": ""}] * 3, output)
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    assert not output.exists()


@pytest.mark.parametrize("size,rate", [("4100x16", 30), ("32x32", 120)])
def test_excessive_resolution_or_fps_is_explicitly_rejected(tmp_path: Path, size: str, rate: int):
    source = _make_video(tmp_path / "excessive.mp4", size=size, rate=rate, audio=False, seconds=0.2)
    with pytest.raises(MediaError) as error:
        probe(source)
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"


def test_existing_output_is_never_overwritten(source: Path, tmp_path: Path):
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"existing accepted result")
    with pytest.raises(MediaError) as error:
        render([{"path": source, "start": 0, "end": 0.3, "caption": ""}], output)
    assert error.value.code == "MEDIA_OUTPUT_EXISTS"
    assert output.read_bytes() == b"existing accepted result"


def test_missing_file_does_not_disclose_path(tmp_path: Path):
    with pytest.raises(MediaError) as error:
        probe(tmp_path / "private-file.mp4")
    assert error.value.code == "MEDIA_INVALID_FILE"
    assert str(tmp_path) not in str(error.value)


def test_failed_encoder_cleans_scratch_without_touching_source(source: Path, tmp_path: Path, monkeypatch):
    original = source.read_bytes()
    output = tmp_path / "failed.mp4"
    original_run = media._run

    def fail_encoder(name, args, **kwargs):
        if kwargs.get("code") == "MEDIA_RENDER_FAILED":
            raise MediaError("MEDIA_RENDER_FAILED", "本地视频合成失败，原始素材未改动。")
        return original_run(name, args, **kwargs)

    monkeypatch.setattr(media, "_run", fail_encoder)
    with pytest.raises(MediaError) as error:
        render([{"path": source, "start": 0, "end": 0.3, "caption": "仅本地验证"}], output)
    assert error.value.code == "MEDIA_RENDER_FAILED"
    assert not output.exists()
    assert not list(tmp_path.glob(".render-*"))
    assert source.read_bytes() == original
