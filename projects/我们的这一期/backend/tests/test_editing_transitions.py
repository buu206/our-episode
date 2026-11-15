"""Real local transitions: pixels, audio, frame timing and protected expression."""
import hashlib
import math
from pathlib import Path
import subprocess

from PIL import ImageChops, ImageStat
import pytest

from app.services import media, variety, variety_v2
from app.services.editing import timeline
from app.services.media import MediaError
from test_packaging import ffmpeg, narration
from test_variety import frame, pcm


def make_source(path: Path, color: str, frequency: int) -> Path:
    subprocess.run([ffmpeg(), "-nostdin", "-v", "error", "-threads", "2",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
        "-t", "1.5", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(path)],
        check=True, capture_output=True, timeout=25)
    return path


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("editing-transitions")
    sources = [make_source(root / f"{color}.mp4", color, freq)
               for color, freq in (("red", 440), ("blue", 660), ("lime", 880), ("white", 1100))]
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    return root, sources, hashes


def mixed_segments(sources, *, captions=False):
    return [{"path": path, "start": 0, "end": 1.2, "caption": f"片段{i+1}" if captions else "",
             "transition": kind, "transition_duration": .4 if kind != "cut" else 0}
            for i, (path, kind) in enumerate(zip(sources, ("cut", "dissolve", "cut", "fade_black")))]


@pytest.fixture(scope="module")
def rendered(samples):
    root, sources, _ = samples
    output = root / "mixed.mp4"
    info = media.render(mixed_segments(sources), output)
    return output, info


def color_at(path, seconds):
    # A region average distinguishes xfade's per-pixel dissolve from a hard cut.
    return ImageStat.Stat(frame(path, seconds).crop((400, 200, 800, 450))).mean


def test_mixed_transitions_are_visible_and_have_expected_total_duration(rendered, samples):
    output, info = rendered
    assert info["duration"] == pytest.approx(4.0, abs=.04)
    assert (info["width"], info["height"], info["has_audio"]) == (1280, 720, True)
    red, dissolve, blue = [color_at(output, time) for time in (.4, 1.0, 1.6)]
    assert red[0] > 240 and red[2] < 10
    assert 70 < dissolve[0] < 190 and 70 < dissolve[2] < 190
    assert blue[2] > 240 and blue[0] < 10
    assert color_at(output, 2.05)[1] > 240  # The cut remains a cut after an earlier dissolve.
    # FFmpeg fadeblack uses its own non-linear curve; black must occur inside
    # the overlap, without assuming it lands at its arithmetic midpoint.
    assert min(max(color_at(output, frame_index / 30)) for frame_index in range(84, 97)) < 10
    assert min(color_at(output, 3.6)) > 240
    _, sources, hashes = samples
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources} == hashes


def test_audio_survives_cuts_and_crossfades_without_gaps_or_clipping(rendered):
    output, _ = rendered
    for start, frequency in ((.2, 440), (1.5, 660), (2.2, 880), (3.5, 1100)):
        audio = pcm(output, start=start, length=.2)
        assert audio and max(abs(value) for value in audio) > 500
        crossings = sum(a <= 0 < b for a, b in zip(audio, audio[1:]))
        assert crossings == pytest.approx(frequency * .2, abs=4)
    for start in (.86, 2.86):
        overlap = pcm(output, start=start, length=.27)
        assert math.sqrt(sum(v*v for v in overlap) / len(overlap)) > 500
    assert max(abs(v) for v in pcm(output)) < 32000


def test_nonframe_lengths_round_consistently_without_extending_selected_source(samples, tmp_path):
    _, sources, _ = samples
    rows = [{"path": sources[0], "start": .1, "end": .851},
            {"path": sources[1], "start": .1, "end": .871,
             "transition": "dissolve", "transition_duration": .22}]
    output = tmp_path / "rounded.mp4"
    result = media.render(rows, output)
    expected = timeline(rows)[-1].end
    assert result["duration"] == pytest.approx(expected, abs=.04)
    assert color_at(output, expected - .10)[2] > 240


@pytest.mark.parametrize("side", [0, 1])
def test_confirmed_interview_cannot_be_crossfaded_on_either_side(samples, tmp_path, side):
    _, sources, _ = samples
    rows = mixed_segments(sources)[:2]
    rows[side]["_preserve_expression"] = True
    output = tmp_path / "protected.mp4"
    with pytest.raises(MediaError) as failure:
        media.render(rows, output)
    assert failure.value.code == "MEDIA_INVALID_SEGMENT"
    assert not output.exists()


@pytest.mark.parametrize("mutation", [
    {"transition": "fade_black", "transition_duration": .6},
    {"transition": "dissolve", "transition_duration": .05},
    {"transition": "cut", "transition_duration": .3},
    {"transition": "dissolve;bad", "transition_duration": .3},
    {"transition": "dissolve", "transition_duration": float("nan")},
])
def test_invalid_transition_parameters_never_publish(samples, tmp_path, mutation):
    _, sources, _ = samples
    rows = mixed_segments(sources)[:2]
    rows[1].update(mutation)
    output = tmp_path / "invalid.mp4"
    with pytest.raises(MediaError) as failure:
        media.render(rows, output)
    assert failure.value.code == "MEDIA_INVALID_SEGMENT" and not output.exists()


def test_first_shot_and_short_neighbor_reject_overlap(samples, tmp_path):
    _, sources, _ = samples
    for rows in (
        [{"path": sources[0], "start": 0, "end": 1, "transition": "dissolve", "transition_duration": .2}],
        [{"path": sources[0], "start": 0, "end": .3},
         {"path": sources[1], "start": 0, "end": 1, "transition": "fade_black", "transition_duration": .2}],
    ):
        with pytest.raises(MediaError) as failure:
            media.render(rows, tmp_path / "invalid.mp4")
        assert failure.value.code == "MEDIA_INVALID_SEGMENT"


def test_transitions_preserve_existing_outputs_and_clean_failed_render(samples, tmp_path, monkeypatch):
    _, sources, _ = samples
    existing = tmp_path / "accepted.mp4"
    existing.write_bytes(b"accepted prior version")
    with pytest.raises(MediaError) as failure:
        media.render(mixed_segments(sources), existing)
    assert failure.value.code == "MEDIA_OUTPUT_EXISTS"
    assert existing.read_bytes() == b"accepted prior version"
    def fail(*args, **kwargs):
        raise MediaError("MEDIA_RENDER_FAILED", "local test failure")
    monkeypatch.setattr(media, "_render_transitions", fail)
    new = tmp_path / "failed.mp4"
    with pytest.raises(MediaError):
        media.render(mixed_segments(sources), new)
    assert not new.exists() and not list(tmp_path.glob(".render-*"))


@pytest.mark.parametrize("renderer", [variety, variety_v2])
def test_variety_layers_use_compressed_timeline_and_avoid_overlap(samples, tmp_path, renderer, monkeypatch):
    _, sources, _ = samples
    rows = mixed_segments(sources, captions=True)
    observed = {}
    decorate = renderer._decorate
    def inspect(*args, **kwargs):
        observed.update(kwargs)
        return decorate(*args, **kwargs)
    monkeypatch.setattr(renderer, "_decorate", inspect)
    output = tmp_path / "packaged.mp4"
    voice = narration(tmp_path / "voice.wav", active=.3, lead=.05, tail=.05)
    info = renderer.render_variety(rows, output, title="转场技术样片", narration=voice)
    offset = observed["offset"]
    assert info["duration"] == pytest.approx(4 + offset, abs=.07)
    windows = variety._caption_windows(observed["captions"], offset, observed["timings"])
    expected = [(offset, offset + .8), (offset + 1.2, offset + 2),
                (offset + 2, offset + 2.8), (offset + 3.2, offset + 4)]
    for actual, wanted in zip(windows, expected):
        assert actual == pytest.approx(wanted)
    if renderer is variety_v2:
        assert len(info["cue_summary"]) == 4
        for cue, (start, end) in zip(info["cue_summary"], windows):
            assert start < cue["start"] < cue["end"] <= end + .001
    # Last caption must be visible in the final shot before its shortened end.
    final = frame(output, offset + 3.6)
    region = final.crop((250, 420, 1030, 640))
    assert ImageChops.difference(region, region.copy().point(lambda _: 255)).getbbox()
    assert pcm(output)
