"""Real local interview fixtures; no model, network or transcript assumptions."""
import array
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from app.services import interview_media as im, media
from app.services.media import MediaError


def ffmpeg(args):
    return subprocess.run([media._binary("ffmpeg"), "-nostdin", "-v", "error", "-threads", "2",
                           "-filter_threads", "1", *args], check=True, capture_output=True, timeout=45).stdout


def audio(path, seconds=1.2):
    codec = {".wav": "pcm_s16le", ".mp3": "libmp3lame", ".m4a": "aac"}[path.suffix]
    ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:a", codec, "-ac", "2", str(path)])
    return path


def video(path, *, seconds=1.2, sound=True, portrait=False):
    args = ["-f", "lavfi", "-i", f"color=c=blue:s={'90x160' if portrait else '160x90'}:r=30:d={seconds}"]
    if sound:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    args += ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p"]
    if sound:
        args += ["-c:a", "aac", "-ac", "2"]
    ffmpeg([*args, str(path)])
    return path


def samples(path, start=0, duration=.2):
    raw = ffmpeg(["-ss", str(start), "-i", str(path), "-t", str(duration),
                  "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"])
    values = array.array("h")
    values.frombytes(raw)
    return values


def frame(path, seconds):
    return ffmpeg(["-ss", str(seconds), "-i", str(path), "-an", "-frames:v", "1",
                   "-vf", "scale=128:72", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])


@pytest.fixture(autouse=True)
def no_python_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("interview helpers must remain local")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("interview-sources").resolve()
    return {suffix: audio(root / ("voice" + suffix)) for suffix in (".wav", ".mp3", ".m4a")} | {
        ".mp4": video(root / "interview.mp4"),
        ".mov": video(root / "portrait.mov", portrait=True),
        "silent": video(root / "silent.mp4", sound=False),
    }


@pytest.mark.parametrize("suffix,mime", [(".wav", "audio/wav"), (".mp3", "audio/mpeg"), (".m4a", "audio/mp4"),
                                         (".mp4", "video/mp4"), (".mov", "video/quicktime")])
def test_probe_real_containers(sources, suffix, mime):
    info = im.probe_interview(sources[suffix], "audio" if suffix in im._AUDIO_FORMATS else "video")
    assert info["duration"] == pytest.approx(1.2, abs=.04)
    assert info["size"] == sources[suffix].stat().st_size
    assert info["media_type"] == mime and info["has_audio"] is True
    assert {"duration", "width", "height", "has_audio", "format"} <= info.keys()
    assert "path" not in info
    if suffix in im._AUDIO_FORMATS:
        assert info["width"] == info["height"] == 0


@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".m4a", ".mp4", ".mov"])
def test_materialize_real_audio_video_with_permanent_literal_label(sources, tmp_path, monkeypatch, suffix):
    source = sources[suffix]
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "interview.mp4"
    labels = []
    original_overlay = media._overlay
    def overlay(path, width, height, caption, title):
        labels.append((caption, title))
        return original_overlay(path, width, height, caption, title)
    monkeypatch.setattr(media, "_overlay", overlay)
    expression = "我只是在听。' ; [v] % : $(touch x)"
    info = im.materialize_interview(source, "audio" if suffix in im._AUDIO_FORMATS else "video", output,
                                    "小李 ' ; [v]", start=.1, end=.9, expression=expression)
    assert info["duration"] == pytest.approx(.8, abs=.04)
    assert (info["width"], info["height"], info["has_audio"], info["media_type"]) == (1280, 720, True, "video/mp4")
    assert labels == [("本人确认的表达：" + expression, "事后采访 · 小李 ' ; [v]")]
    spec = im._metadata(output, "mov")["streams"]
    sound = next(s for s in spec if s["codec_type"] == "audio")
    assert (sound["codec_name"], sound["sample_rate"], sound["channels"]) == ("aac", "48000", 2)
    for seconds in (.1, .6):
        values = samples(output, seconds, .1)
        assert values and max(abs(v) for v in values) > 100
        picture = frame(output, seconds)
        assert len(picture) == 128 * 72 * 3
        # Identity panel stays in the top band through the selected span, and
        # the expression panel stays in the bottom band (not only a title card).
        for row in (5, 65):
            pixel = picture[(row * 128 + 8) * 3:(row * 128 + 8) * 3 + 3]
            assert max(pixel) < 190
    ffmpeg(["-xerror", "-i", str(output), "-f", "null", "-"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert not (tmp_path / "x").exists()
    assert not list(tmp_path.glob(".interview-*"))


def test_silent_video_is_inspectable_but_not_a_voiced_interview(sources, tmp_path):
    assert im.probe_interview(sources["silent"], "video")["has_audio"] is False
    with pytest.raises(MediaError) as error:
        im.materialize_interview(sources["silent"], "video", tmp_path / "result.mp4", "同学")
    assert error.value.code == "INTERVIEW_AUDIO_REQUIRED"
    assert not (tmp_path / "result.mp4").exists()


@pytest.mark.parametrize("source_suffix,target_suffix,kind", [(".wav", ".mp3", "audio"),
    (".mp3", ".wav", "audio"), (".mp4", ".m4a", "audio"), (".wav", ".mp4", "video")])
def test_disguised_container_rejected(sources, tmp_path, source_suffix, target_suffix, kind):
    path = tmp_path / ("private-name" + target_suffix)
    path.write_bytes(sources[source_suffix].read_bytes())
    with pytest.raises(MediaError) as error:
        im.probe_interview(path, kind)
    assert str(tmp_path) not in error.value.message and "private-name" not in error.value.message


def test_path_protocol_symlink_size_and_duration_limits(tmp_path, sources):
    for path in ["https://example.invalid/voice.wav", Path("relative.wav"), tmp_path / "missing.wav"]:
        with pytest.raises(MediaError):
            im.probe_interview(path)
    link = tmp_path / "symlink.wav"
    link.symlink_to(sources[".wav"])
    with pytest.raises(MediaError):
        im.probe_interview(link)
    parent_link = tmp_path / "linked"
    parent_link.symlink_to(sources[".wav"].parent, target_is_directory=True)
    with pytest.raises(MediaError):
        im.probe_interview(parent_link / sources[".wav"].name)
    huge = tmp_path / "huge.wav"
    with huge.open("wb") as file:
        file.truncate(im.MAX_INTERVIEW_BYTES + 1)
    with pytest.raises(MediaError) as error:
        im.probe_interview(huge)
    assert error.value.code == "INTERVIEW_LIMIT_EXCEEDED"
    long = audio(tmp_path / "long.wav", seconds=60.1)
    with pytest.raises(MediaError) as error:
        im.probe_interview(long)
    assert error.value.code == "INTERVIEW_LIMIT_EXCEEDED"


@pytest.mark.parametrize("suffix", [".wav", ".mp3", ".m4a"])
def test_sixty_second_audio_is_inclusive_despite_codec_padding(tmp_path, suffix):
    path = audio(tmp_path / ("at-limit" + suffix), seconds=60)
    info = im.probe_interview(path)
    assert info["duration"] == pytest.approx(60, abs=.001)


def test_truncated_audio_and_remote_playlist_rejected(tmp_path, sources):
    truncated = tmp_path / "truncated.wav"
    original = sources[".wav"].read_bytes()
    truncated.write_bytes(original[:len(original) // 2])
    playlist = tmp_path / "playlist.mp3"
    playlist.write_text("#EXTM3U\n#EXTINF:1\nhttps://example.invalid/private.mp3\n", encoding="utf-8")
    for path in (truncated, playlist):
        with pytest.raises(MediaError):
            im.probe_interview(path)


@pytest.mark.parametrize("start,end", [(-1, .5), (.5, .5), (0, 5), (float("nan"), 1), (True, 1)])
def test_invalid_adoption_span_does_not_publish(sources, tmp_path, start, end):
    output = tmp_path / "invalid.mp4"
    with pytest.raises(MediaError) as error:
        im.materialize_interview(sources[".wav"], "audio", output, "我", start, end)
    assert error.value.code == "MEDIA_INVALID_SEGMENT" and not output.exists()


@pytest.mark.parametrize("label,expression", [("", ""), ("甲" * 41, ""), ("甲", "乙" * 101), ("甲\n乙", ""), ("甲", "\x00"), ("\u202e甲", "")])
def test_invalid_or_unbounded_text_rejected(sources, tmp_path, label, expression):
    with pytest.raises(MediaError) as error:
        im.materialize_interview(sources[".wav"], "audio", tmp_path / "invalid.mp4", label, expression=expression)
    assert error.value.code == "MEDIA_INVALID_TEXT"


def test_context_only_contains_the_requested_interval(tmp_path):
    source = tmp_path / "context-source.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=160x90:r=30:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-vf", "drawbox=c=red:t=fill:enable='lt(t,1)',drawbox=c=green:t=fill:enable='between(t,1,2)'",
            "-metadata", "comment=private source metadata", "-c:v", "libx264", "-threads", "2",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)])
    output = tmp_path / "context.mp4"
    info = im.context_clip(source, output, 1.1, 1.8)
    assert info["duration"] == pytest.approx(.7, abs=.04)
    assert info["width"] == 1280 and info["height"] == 720
    for seconds in (.05, .6):
        picture = frame(output, seconds)
        center = picture[(36 * 128 + 64) * 3:(36 * 128 + 64) * 3 + 3]
        assert center[1] > 90 and center[0] < 35 and center[2] < 35
    assert max(abs(v) for v in samples(output)) > 100
    metadata = subprocess.run([media._binary("ffprobe"), "-v", "error", "-show_format", "-show_chapters",
                               "-of", "json", str(output)], check=True, capture_output=True, timeout=20).stdout
    assert b"private source metadata" not in metadata
    assert json.loads(metadata)["chapters"] == []
    assert not list(tmp_path.glob(".interview-*"))


def test_context_cap_and_silent_source(sources, tmp_path, monkeypatch):
    original_probe = media.probe
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 40})
    with pytest.raises(MediaError) as error:
        im.context_clip(sources[".mp4"], tmp_path / "too-long.mp4", 0, 30.01)
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    monkeypatch.setattr(media, "probe", original_probe)
    output = tmp_path / "silent-context.mp4"
    info = im.context_clip(sources["silent"], output, .1, .6)
    assert info["duration"] == pytest.approx(.5, abs=.04)
    assert max(abs(v) for v in samples(output)) < 20


def test_exclusive_publish_and_failure_cleanup(sources, tmp_path, monkeypatch):
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"leave unchanged")
    with pytest.raises(MediaError) as error:
        im.materialize_interview(sources[".wav"], "audio", output, "小李")
    assert error.value.code == "MEDIA_OUTPUT_EXISTS" and output.read_bytes() == b"leave unchanged"
    target = tmp_path / "racing.mp4"
    original_link = im.os.link
    def racing_link(source, destination):
        Path(destination).write_bytes(b"other completed job")
        return original_link(source, destination)
    monkeypatch.setattr(im.os, "link", racing_link)
    with pytest.raises(MediaError) as error:
        im.materialize_interview(sources[".wav"], "audio", target, "小李", 0, .3)
    assert error.value.code == "MEDIA_OUTPUT_EXISTS"
    assert target.read_bytes() == b"other completed job"
    assert not list(tmp_path.glob(".interview-*"))
    def failed(*args, **kwargs):
        raise MediaError("MEDIA_RENDER_FAILED", "controlled failure")
    monkeypatch.setattr(media, "_overlay", failed)
    with pytest.raises(MediaError):
        im.materialize_interview(sources[".wav"], "audio", tmp_path / "failed.mp4", "小李")
    assert not (tmp_path / "failed.mp4").exists() and not list(tmp_path.glob(".interview-*"))


@pytest.mark.parametrize("source_key", [".mp4", ".mov", "silent", "legacy-mov"])
def test_contribution_preview_normalises_without_interview_labels(sources, tmp_path, monkeypatch, source_key):
    if source_key == "legacy-mov":
        source = tmp_path / "mpeg4-pcm.mov"
        ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=160x90:r=30:d=1.2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2",
                "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(source)])
    else:
        source = sources[source_key]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    def no_interview_overlay(*args, **kwargs):
        raise AssertionError("Ordinary contributions must not get interview text")
    monkeypatch.setattr(media, "_overlay", no_interview_overlay)
    output = tmp_path / "preview.mp4"
    info = im.preview_contribution(source, output)
    assert info["duration"] == pytest.approx(1.2, abs=.04)
    assert (info["width"], info["height"], info["media_type"]) == (1280, 720, "video/mp4")
    tracks = im._metadata(output, "mov")["streams"]
    assert next(s for s in tracks if s["codec_type"] == "video")["codec_name"] == "h264"
    sound = next(s for s in tracks if s["codec_type"] == "audio")
    assert (sound["codec_name"], sound["sample_rate"], sound["channels"]) == ("aac", "48000", 2)
    amplitude = max(abs(v) for v in samples(output, .2))
    assert amplitude < 20 if source_key == "silent" else amplitude > 100
    for seconds in (.1, 1.0):
        picture = frame(output, seconds)
        for row in (5, 65):
            pixel = picture[(row * 128 + 64) * 3:(row * 128 + 64) * 3 + 3]
            assert pixel[2] > 190 and pixel[0] < 30
    ffmpeg(["-xerror", "-i", str(output), "-f", "null", "-"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert not list(tmp_path.glob(".interview-*"))


@pytest.mark.parametrize("seconds,size,accepted", [
    (60.1, 100_000_001, True), (600.0, 1024**3, True),
    (600.1, 1024, False), (1.0, 1024**3 + 1, False),
])
def test_contribution_preview_uses_ordinary_source_limits_without_large_files(
        sources, tmp_path, monkeypatch, seconds, size, accepted):
    """Exercise real media._inspect limits with bounded metadata/stat substitutes.

    Real decoding/rendering is covered above; no ten-minute or gigabyte fixture
    is created merely to test metadata boundary decisions.
    """
    source = sources[".mp4"]
    original_stat = Path.stat
    def reported_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == source:
            values = list(result)
            values[6] = size
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, "stat", reported_stat)
    metadata = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": str(seconds),
                           "tags": {"major_brand": "isom"}},
                "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                             "width": 160, "height": 90, "duration": str(seconds),
                             "avg_frame_rate": "30/1", "disposition": {"attached_pic": 0}}]}
    def bounded_probe(name, *args, **kwargs):
        assert name == "ffprobe"
        return json.dumps(metadata).encode()
    monkeypatch.setattr(media, "_run", bounded_probe)
    monkeypatch.setattr(media, "_decode_sample", lambda source: None)
    composed = []
    def capture_compose(source, output, start, end, **options):
        composed.append((start, end, options))
        return {"duration": end - start}
    monkeypatch.setattr(im, "_compose", capture_compose)
    output = tmp_path / "preview.mp4"
    if accepted:
        assert im.preview_contribution(source, output)["duration"] == seconds
        assert composed == [(0.0, seconds, {"kind": "video"})]
    else:
        with pytest.raises(MediaError) as error:
            im.preview_contribution(source, output)
        assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
        assert not composed and not output.exists()


def test_contribution_preview_still_rejects_audio_only_suffix(sources, tmp_path):
    with pytest.raises(MediaError) as error:
        im.preview_contribution(sources[".wav"], tmp_path / "not-video.mp4")
    assert error.value.code == "MEDIA_INVALID_FILE"
