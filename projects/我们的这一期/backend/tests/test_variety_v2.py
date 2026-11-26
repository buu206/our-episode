"""Real local encodes: versioned emphasis, sparse accents, literal text and audio."""

import hashlib
from pathlib import Path
import subprocess

from PIL import ImageChops
import pytest

from app.services import variety_v2 as variety
from app.services.media import MediaError
from test_packaging import ffmpeg, make_cut, narration
from test_variety import frame, pcm


def source_video(path: Path, *, size="720x1280", audio=True):
    args = [ffmpeg(), "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000"]
    args += ["-t", "2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
    subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=25)
    return path


@pytest.fixture(scope="module")
def packaged(tmp_path_factory):
    folder = tmp_path_factory.mktemp("variety-v2-encode")
    source = source_video(folder / "source.mp4")
    voice = narration(folder / "voice.wav", active=.6, lead=.05, tail=.05)
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, voice)}
    captions = ["桌边同框，左侧有人站立", "坐于桌旁，手举至面部", "俯身靠近桌面"]
    output = folder / "variety-v2.mp4"
    result = variety.render_variety([
        {"path": source, "start": 0, "end": 2, "caption": text} for text in captions
    ], output, title="在场技术预览", narration=voice)
    return folder, source, voice, hashes, output, result


def test_three_layouts_have_entry_emphasis_and_clean_intervals(packaged):
    folder, _, _, hashes, output, result = packaged
    assert (result["width"], result["height"], result["has_audio"]) == (720, 1280, True)
    assert 6.7 < result["duration"] < 6.85
    assert result["packaging"] == "variety_v2" and result["renderer_revision"] == "variety-v2.3"
    assert result["cue_count"] == 3
    assert [cue["style"] for cue in result["cue_summary"]] == ["impact", "aside", "stamp"]
    # Fixed blue media: changes are the timed graphic pass, not camera movement.
    for cue in result["cue_summary"]:
        clean = frame(output, cue["start"] - .12)
        emphasis = frame(output, cue["emphasis"] + .35)
        after = frame(output, cue["end"] + .08)
        roi = (30, 870, 690, 1125)
        assert ImageChops.difference(clean.crop(roi), emphasis.crop(roi)).getbbox()
        assert ImageChops.difference(after.crop(roi), emphasis.crop(roi)).getbbox()
        for rect in ((100, 300, 620, 820), (60, 1180, 660, 1250)):
            image = emphasis.crop(rect)
            assert image.getextrema()[0][1] < 9 and image.getextrema()[2][0] > 240
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes} == hashes
    assert not list(folder.glob(".variety-v2-*"))


def test_portrait_opening_keeps_upper_middle_faces_clear(packaged):
    _, _, _, _, output, _ = packaged
    opening = frame(output, .4)
    # The intro is a held source frame; only the lower half carries large type.
    for roi in ((100, 300, 620, 650), (80, 1180, 640, 1250)):
        clean = opening.crop(roi)
        assert clean.getextrema()[0][1] < 9 and clean.getextrema()[2][0] > 240
    assert opening.crop((80, 690, 640, 1120)).getextrema()[0][1] > 200


def test_actual_motion_is_not_a_static_caption(packaged):
    _, _, _, _, output, result = packaged
    cue = result["cue_summary"][0]
    entry = frame(output, cue["start"] + .05).crop((30, 870, 690, 1125))
    settled = frame(output, cue["start"] + .45).crop((30, 870, 690, 1125))
    assert ImageChops.difference(entry, settled).getbbox()


def test_narration_and_original_audio_keep_level_and_do_not_clip(packaged):
    _, _, _, _, output, result = packaged
    opening = pcm(output, start=.12, length=.4)
    scene = pcm(output, start=1.6, length=.35)
    assert sum(value * value for value in opening) / len(opening) > 1_000_000
    assert sum(value * value for value in scene) / len(scene) > 500_000
    assert max(abs(value) for value in pcm(output)) < 32500
    assert 285 < sum(a <= 0 < b for a, b in zip(scene, scene[1:])) < 335
    assert result["sound_accents"] <= 4


def test_full_literal_words_preserved_and_negative_or_quoted_text_not_reframed():
    captions = ["大家同框，旁边有人站着", "坐在一旁，手举到脸旁", "低头靠近桌子"]
    cues = variety.plan_cues([(2, text, False) for text in captions], 0)
    for text, cue in zip(captions, cues):
        words = text.split("，")
        assert (cue.main + cue.detail if cue.style != "aside" else cue.detail + cue.main) == "".join(words)
        assert .15 <= cue.start % 2 <= .3
        assert cue.end - cue.emphasis >= 1.25
    for text in ('不是在争吵，是在聊天', '他说“不要碰杯”', '没有人在发言', '先出现\n再出现'):
        main, detail = variety._parts(text, "aside")
        assert main == text and detail == ""
        cue = variety.plan_cues([(2, text, False)], 0)[0]
        assert cue.style == "literal" and cue.main == text


def test_long_and_command_like_caption_fit_without_becoming_shell_code(tmp_path):
    text = "只是技术排版验证" * 50
    image = variety._literal(text, 638, 235)
    assert image.height <= 235
    assert image.getbbox()
    image = variety._literal("$(touch injected) `echo test`", 638, 235)
    assert image.getbbox() and not (tmp_path / "injected").exists()


def test_protected_expression_has_no_graphics_or_added_sound(tmp_path):
    source = make_cut(tmp_path / "protected.mp4", audio=False)
    output = tmp_path / "protected-output.mp4"
    result = variety.render_variety([{"path": source, "start": 0, "end": 1,
                                      "caption": "确认过的原话", "_preserve_expression": True}], output,
                                     title="不要覆盖身份条")
    assert result["cue_count"] == 0 and result["sound_accents"] == 0
    image = frame(output, .5)
    assert image.getextrema()[0][1] < 9 and image.getextrema()[2][0] > 240
    assert max(abs(value) for value in pcm(output)) < 10


def test_landscape_has_sparse_sound_and_clear_subject_and_player_area(tmp_path):
    source = source_video(tmp_path / "silent.mp4", size="1280x720", audio=False)
    output = tmp_path / "landscape.mp4"
    result = variety.render_variety([{"path": source, "start": 0, "end": 2, "caption": "一同出发"}], output,
                                   title="现场的这一刻")
    assert (result["width"], result["height"]) == (1280, 720)
    assert max(abs(value) for value in pcm(output, start=0, length=.17)) < 10
    assert max(abs(value) for value in pcm(output, start=.23, length=.18)) > 100
    assert max(abs(value) for value in pcm(output, start=.6, length=.5)) < 10
    image = frame(output, 1)
    assert image.crop((200, 80, 1080, 140)).getextrema()[0][1] > 100
    for rect in ((150, 180, 1100, 350), (80, 665, 1200, 705)):
        clean = image.crop(rect)
        assert clean.getextrema()[0][1] < 9 and clean.getextrema()[2][0] > 240


def test_three_original_sound_types_and_dense_count_are_bounded(tmp_path):
    path = tmp_path / "accents.wav"
    assert variety._sfx(path, 15, [(i / 10, ["impact", "aside", "stamp"][i % 3]) for i in range(150)]) == 12
    assert 1000 < max(abs(value) for value in pcm(path)) < 3300
    hashes = []
    for kind in ("impact", "aside", "stamp"):
        out = tmp_path / f"{kind}.wav"
        variety._sfx(out, 1, [(.2, kind)])
        hashes.append(hashlib.sha256(out.read_bytes()).hexdigest())
        assert max(abs(value) for value in pcm(out, start=.5, length=.4)) < 2
    assert len(set(hashes)) == 3


def test_immutable_output_and_failed_pass_cleanup(packaged, tmp_path, monkeypatch):
    _, source, _, hashes, _, _ = packaged
    old = tmp_path / "old.mp4"
    old.write_bytes(b"keep")
    with pytest.raises(MediaError) as error:
        variety.render_variety([], old)
    assert error.value.code == "MEDIA_OUTPUT_EXISTS" and old.read_bytes() == b"keep"
    def fail(*args, **kwargs):
        raise MediaError("MEDIA_RENDER_FAILED", "injected local error")
    monkeypatch.setattr(variety, "_decorate", fail)
    new = tmp_path / "new.mp4"
    with pytest.raises(MediaError):
        variety.render_variety([{"path": source, "start": 0, "end": .6, "caption": "现场"}], new)
    assert not new.exists() and not list(tmp_path.glob(".variety-v2-*"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == hashes[source]


@pytest.mark.parametrize("caption", ["x" * 401, "bad\x00text", 42])
def test_invalid_caption_is_rejected_without_source_access(tmp_path, caption):
    with pytest.raises(MediaError) as error:
        variety.render_variety([{"path": tmp_path / "absent.mp4", "start": 0, "end": 1, "caption": caption}],
                               tmp_path / "output.mp4")
    assert error.value.code == "MEDIA_INVALID_TEXT"
