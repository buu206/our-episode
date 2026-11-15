"""Local integration: rendered motion/graphics, sparse sound and immutable inputs."""

import array
import hashlib
from io import BytesIO
from pathlib import Path
import subprocess

from PIL import Image, ImageChops
import pytest

from app.services import media, variety
from app.services.media import MediaError
from test_packaging import ffmpeg, make_cut, narration


def frame(path, time):
    result = subprocess.run([ffmpeg(), "-nostdin", "-v", "error", "-ss", str(time), "-i", str(path),
                             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                            capture_output=True, check=True, timeout=20)
    return Image.open(BytesIO(result.stdout)).convert("RGB")


def pcm(path, *, start=0, length=None):
    args = [ffmpeg(), "-nostdin", "-v", "error", "-ss", str(start), "-i", str(path)]
    if length is not None:
        args += ["-t", str(length)]
    result = subprocess.run(args + ["-vn", "-ar", "48000", "-ac", "1", "-f", "s16le", "pipe:1"],
                            capture_output=True, check=True, timeout=20)
    values = array.array("h")
    values.frombytes(result.stdout)
    return values


@pytest.fixture(scope="module")
def packaged(tmp_path_factory):
    folder = tmp_path_factory.mktemp("variety-real-encode")
    source = make_cut(folder / "source.mp4", size="720x1280")
    voice = narration(folder / "voice.wav", active=.6, lead=.05, tail=.05)
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, voice)}
    output = folder / "variety.mp4"
    result = variety.render_variety([
        {"path": source, "start": 0, "end": 1, "caption": "镜头给到这一桌"},
        {"path": source, "start": 0, "end": 1, "caption": "第二个现场片段"},
    ], output, title="桌边同框", narration=voice)
    return folder, source, voice, hashes, output, result


def test_portrait_variety_has_real_dynamic_graphics_and_safe_caption_area(packaged):
    folder, source, voice, hashes, output, result = packaged
    assert (result["width"], result["height"], result["has_audio"]) == (720, 1280, True)
    assert 2.7 < result["duration"] < 2.85
    assert result["packaging"] == "variety" and 1 <= result["sound_accents"] <= 3
    intro, early, held = frame(output, .3), frame(output, .94), frame(output, 1.3)
    assert intro.crop((120, 350, 600, 690)).getextrema()[0][1] > 200
    # Fixed blue source: a changed lower third proves motion, not a moving subject.
    roi = (40, 960, 680, 1140)
    assert ImageChops.difference(early.crop(roi), held.crop(roi)).getbbox()
    # No graphics in the subject area or under the bottom player controls.
    center = held.crop((250, 300, 470, 850))
    bottom = held.crop((80, 1180, 640, 1250))
    for image in (center, bottom):
        assert image.getextrema()[0][1] < 8
        assert image.getextrema()[2][0] > 240
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes} == hashes
    assert not list(folder.glob(".variety-*"))


def test_real_voice_and_source_audio_survive_without_clipping(packaged):
    _, _, _, _, output, _ = packaged
    opening, scene = pcm(output, start=.12, length=.4), pcm(output, start=1.1, length=.4)
    assert sum(value * value for value in opening) / len(opening) > 1_000_000
    assert sum(value * value for value in scene) / len(scene) > 500_000
    assert max(abs(value) for value in pcm(output)) < 32500
    # The original 880 Hz source remains dominant; SFX are not replacing speech.
    zero_crossings = sum(a <= 0 < b for a, b in zip(scene, scene[1:]))
    assert 330 < zero_crossings < 380


def test_silent_source_has_only_brief_sfx_not_a_music_bed(tmp_path):
    source = make_cut(tmp_path / "silent.mp4", audio=False)
    output = tmp_path / "variety.mp4"
    result = variety.render_variety([{"path": source, "start": 0, "end": 1, "caption": "现场"}], output)
    assert (result["width"], result["height"]) == (1280, 720)
    assert max(abs(value) for value in pcm(output, length=.15)) > 100
    assert max(abs(value) for value in pcm(output, start=.3, length=.5)) < 10


def test_confirmed_expression_has_no_covering_graphics_or_added_sfx(tmp_path):
    source = make_cut(tmp_path / "protected.mp4", audio=False)
    output = tmp_path / "variety.mp4"
    result = variety.render_variety([{"path": source, "start": 0, "end": 1, "caption": "",
                                      "_preserve_expression": True}], output, title="不可遮盖原有身份条")
    assert result["sound_accents"] == 0
    image = frame(output, .5)
    assert image.getextrema()[0][1] < 8 and image.getextrema()[2][0] > 240
    assert max(abs(value) for value in pcm(output)) < 10


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "old.mp4"
    output.write_bytes(b"preserve")
    with pytest.raises(MediaError) as error:
        variety.render_variety([], output)
    assert error.value.code == "MEDIA_OUTPUT_EXISTS"
    assert output.read_bytes() == b"preserve"


@pytest.mark.parametrize("bad", ["x" * 401, "bad\x00text", 4])
def test_unsafe_caption_is_rejected_before_source_access(tmp_path, bad):
    with pytest.raises(MediaError) as error:
        variety.render_variety([{"path": tmp_path / "missing.mp4", "start": 0, "end": 1, "caption": bad}], tmp_path / "new.mp4")
    assert error.value.code == "MEDIA_INVALID_TEXT"
    assert not (tmp_path / "new.mp4").exists()


def test_sfx_count_and_peak_remain_bounded_for_dense_edits(tmp_path):
    output = tmp_path / "sound.wav"
    assert variety._sfx(output, 20, [index / 10 for index in range(200)]) == 14
    values = pcm(output)
    assert 1500 < max(abs(value) for value in values) < 2100


def test_failed_effect_pass_cleans_scratch_and_never_publishes(packaged, tmp_path, monkeypatch):
    _, source, _, _, _, _ = packaged
    def fail(*args, **kwargs):
        raise MediaError("MEDIA_RENDER_FAILED", "injected local failure")
    monkeypatch.setattr(variety, "_decorate", fail)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(MediaError):
        variety.render_variety([{"path": source, "start": 0, "end": .4, "caption": "现场"}], tmp_path / "new.mp4")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not (tmp_path / "new.mp4").exists()
    assert not list(tmp_path.glob(".variety-*"))
