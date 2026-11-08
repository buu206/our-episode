"""Real local media checks for a separate narrated intro and unchanged AAC sound."""

import array
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from app.services import media, packaging
from app.services.media import MediaError
from app.services.packaging import add_narrated_intro


def ffmpeg():
    path = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(path).is_file():
        pytest.skip("FFmpeg unavailable")
    return path


def make_cut(path, *, size="1280x720", audio=True):
    args = [ffmpeg(), "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000"]
    args += ["-t", "1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
    args += [str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=25)
    return path


def narration(path, *, active=1, lead=1, tail=1):
    rate = 24000
    values = array.array("h", [0] * round(lead * rate))
    values.extend(round(6000 * math.sin(2 * math.pi * 440 * sample / rate)) for sample in range(round(active * rate)))
    values.extend([0] * round(tail * rate))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(values.tobytes())
    return path


def packets(path):
    probe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    result = subprocess.run([probe, "-v", "error", "-select_streams", "a:0", "-show_packets",
                             "-show_entries", "packet=pts_time,data_hash", "-show_data_hash", "sha256", "-of", "json", str(path)],
                            check=True, capture_output=True, timeout=20)
    return json.loads(result.stdout)["packets"]


@pytest.fixture(scope="module")
def landscape(tmp_path_factory):
    return make_cut(tmp_path_factory.mktemp("packaging-source") / "cut.mp4")


def test_intro_preserves_every_original_aac_packet_and_limits_silence(landscape, tmp_path):
    voice = narration(tmp_path / "voice.wav")
    output = tmp_path / "new-version.mp4"
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (landscape, voice)}
    result = add_narrated_intro(landscape, voice, output, title="我们的这一期")
    assert result["width"] == 1280 and result["height"] == 720
    assert 2.1 <= result["duration"] <= 2.3
    original, combined = packets(landscape), packets(output)
    # Stronger than waveform similarity: the original compressed audio payloads
    # are preserved exactly, including their ordering, through final packaging.
    assert [row["data_hash"] for row in combined[-len(original):]] == [row["data_hash"] for row in original]
    boundary = float(combined[-len(original)]["pts_time"])
    pcm = subprocess.run([ffmpeg(), "-nostdin", "-v", "error", "-i", str(output), "-t", str(boundary),
                          "-vn", "-ac", "1", "-ar", "24000", "-f", "s16le", "pipe:1"],
                         check=True, capture_output=True, timeout=20).stdout
    samples = array.array("h")
    samples.frombytes(pcm)
    active = [index for index, value in enumerate(samples) if abs(value) > 100]
    assert active and active[0] / 24000 <= 0.3
    assert (len(samples) - 1 - active[-1]) / 24000 <= 0.3
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes} == hashes
    assert not list(tmp_path.glob(".packaging-*"))


def test_portrait_and_literal_title_are_rendered_without_command_execution(tmp_path, monkeypatch):
    cut = make_cut(tmp_path / "cut 'literal'.mp4", size="720x1280")
    voice = narration(tmp_path / "voice.wav", lead=0, tail=0, active=0.5)
    output = tmp_path / "new.mp4"
    marker = tmp_path / "injected"
    monkeypatch.chdir(tmp_path)
    result = add_narrated_intro(cut, voice, output, title="$(touch 'injected')")
    assert (result["width"], result["height"]) == (720, 1280)
    assert not marker.exists()
    probe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    metadata = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                               "-of", "json", str(output)], check=True, capture_output=True, timeout=20)
    assert json.loads(metadata.stdout)["streams"][0]["avg_frame_rate"] == "30/1"


def test_missing_original_audio_is_rejected(tmp_path):
    cut = make_cut(tmp_path / "silent.mp4", audio=False)
    voice = narration(tmp_path / "voice.wav")
    with pytest.raises(MediaError) as error:
        add_narrated_intro(cut, voice, tmp_path / "new.mp4")
    assert error.value.code == "PACKAGING_AUDIO_REQUIRED"


def test_existing_output_is_never_overwritten(landscape, tmp_path):
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"existing-data")
    with pytest.raises(MediaError) as error:
        add_narrated_intro(landscape, tmp_path / "not-needed.wav", output)
    assert error.value.code == "MEDIA_OUTPUT_EXISTS"
    assert output.read_bytes() == b"existing-data"


@pytest.mark.parametrize("mode", ["silence", "too-long", "truncated"])
def test_invalid_narration_never_creates_output(landscape, tmp_path, mode):
    voice = tmp_path / "voice.wav"
    if mode == "silence":
        narration(voice, active=0)
    elif mode == "too-long":
        narration(voice, active=31, lead=0, tail=0)
    else:
        narration(voice)
        voice.write_bytes(voice.read_bytes()[:-10])
    with pytest.raises(MediaError):
        add_narrated_intro(landscape, voice, tmp_path / "new.mp4")
    assert not (tmp_path / "new.mp4").exists()
    assert not list(tmp_path.glob(".packaging-*"))


def test_total_limit_is_checked_before_encoding(landscape, tmp_path, monkeypatch):
    original = media._inspect
    monkeypatch.setattr(media, "_inspect", lambda path: replace(original(path), duration=89.5))
    with pytest.raises(MediaError) as error:
        add_narrated_intro(landscape, narration(tmp_path / "voice.wav"), tmp_path / "new.mp4")
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    assert not list(tmp_path.glob(".packaging-*"))


def test_failure_cleans_scratch_and_preserves_sources(landscape, tmp_path, monkeypatch):
    voice = narration(tmp_path / "voice.wav")
    original = media._run
    def fail_encoding(name, args, **kwargs):
        if args[-1].endswith("intro.mp4"):
            raise MediaError("MEDIA_RENDER_FAILED", "offline injected failure")
        return original(name, args, **kwargs)
    monkeypatch.setattr(media, "_run", fail_encoding)
    before = hashlib.sha256(landscape.read_bytes()).hexdigest()
    with pytest.raises(MediaError):
        add_narrated_intro(landscape, voice, tmp_path / "new.mp4")
    assert hashlib.sha256(landscape.read_bytes()).hexdigest() == before
    assert not (tmp_path / "new.mp4").exists()
    assert not list(tmp_path.glob(".packaging-*"))
