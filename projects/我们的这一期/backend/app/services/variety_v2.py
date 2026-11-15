"""Second, versioned variety treatment: emphasis, annotation and breathing room.

Only the approved literal captions are typeset. No dialogue, inner thoughts,
reactions or relationships are inferred. Images and brief sound accents are
drawn/synthesised locally. The v1 renderer remains available for old versions.
"""

from __future__ import annotations

import array
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import wave

from PIL import Image, ImageDraw

from . import media
from .editing import ClipTiming, timeline
from .media import MediaError
from .packaging import _narration, add_narrated_intro
from .variety import _caption_windows, _font, _text

REVISION = "variety-v2.2"
INK = "#251b23"
PAPER = "#fff8e8"
YELLOW = "#ffdc4c"
CORAL = "#ff6d83"
ORANGE = "#ff9855"
TEAL = "#8fe1d2"


@dataclass(frozen=True)
class Cue:
    segment: int
    start: float
    emphasis: float
    end: float
    style: str
    main: str
    detail: str


def _needs_literal(text: str) -> bool:
    # Avoid changing the apparent speaker, negation or quotation scope.
    return (len(text) > 28 or any(char in text for char in '“”"「」『』\n\t')
            or any(word in text for word in ("没有", "不是", "不再", "并非", "不要")))


def _parts(text: str, style: str) -> tuple[str, str]:
    """Split, never paraphrase. Long or quoted content uses literal typesetting."""
    clean = text.strip()
    if _needs_literal(clean):
        return clean, ""
    clauses = re.split(r"[，,；;。]\s*", clean, maxsplit=1)
    if len(clauses) == 2 and clauses[0] and clauses[1]:
        # A line break replaces the clause separator; letters/words stay literal.
        if style == "aside":
            return clauses[1], clauses[0]
        return clauses[0], clauses[1]
    if style == "stamp" and 4 <= len(clean) <= 10:
        return clean[:2], clean[2:]
    return clean, ""


def plan_cues(captions: list[tuple[float, str, bool]], offset: float,
              timings: list[ClipTiming] | None = None) -> list[Cue]:
    """Fixed editorial timing, not an event detector. Never touch protected cuts."""
    windows = _caption_windows(captions, offset, timings)
    cues = []
    for index, (_, caption, protected) in enumerate(captions):
        cursor, boundary = windows[index]
        duration = max(0, boundary - cursor)
        if not protected and caption.strip() and duration >= .45:
            style = ("impact", "aside", "stamp")[index % 3]
            main, detail = _parts(caption, style)
            if _needs_literal(caption) or len(main) > 12 or len(detail) > 18:
                style, main, detail = "literal", caption.strip(), ""
            lead = min(.22, duration * .16)
            tail = min(.17, duration * .12)
            start = cursor + lead
            emphasis = start + (min(.17, duration * .08) if style == "aside" and detail else 0)
            # Do not show the packaging for an entire long shot.
            end = min(cursor + duration - tail, start + max(2.1, len(caption) / 8))
            cues.append(Cue(index, start, emphasis, end, style, main, detail))
    return cues


def _outlined(text: str, size: int, color: str = PAPER, *, tilt: float = 0,
              shadow: str = INK, width: int | None = None) -> Image.Image:
    font = _font(size, text)
    while width and font.getlength(text) + 36 > width and size > 14:
        size -= 2
        font = _font(size, text)
    bounds = font.getbbox(text)
    canvas = Image.new("RGBA", (math.ceil(font.getlength(text)) + 44, bounds[3] - bounds[1] + 48))
    draw = ImageDraw.Draw(canvas)
    pos = (20, 15 - bounds[1])
    stroke = max(2, round(size / 18))
    for distance in range(8, 0, -1):
        draw.text((pos[0] + distance, pos[1] + distance), text, font=font,
                  fill=shadow, stroke_width=stroke + 3, stroke_fill=shadow)
    draw.text(pos, text, font=font, fill=color, stroke_width=stroke + 3, stroke_fill=PAPER)
    draw.text(pos, text, font=font, fill=color, stroke_width=stroke, stroke_fill=INK)
    return canvas.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True)


def _small(text: str, width: int, *, size: int = 29) -> Image.Image:
    image = _outlined(text, size, PAPER, width=width)
    if image.width > width:
        ratio = width / image.width
        image = image.resize((width, max(1, round(image.height * ratio))), Image.Resampling.LANCZOS)
    return image


def _literal(text: str, width: int, height: int) -> Image.Image:
    """Keep every character when an elaborate layout would change its scope."""
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    for size in range(38, 13, -2):
        font = _font(size, text)
        lines = media._wrap(text, dummy, font, width - 38)
        if len(lines) * (size + 6) + 20 <= height:
            break
    else:
        raise MediaError("MEDIA_INVALID_TEXT", "花字过长，无法完整放入安全区域，请缩短后重试。")
    result = Image.new("RGBA", (width, len(lines) * (size + 6) + 20))
    draw = ImageDraw.Draw(result)
    for i, line in enumerate(lines):
        x = (width - draw.textlength(line, font=font)) / 2
        draw.text((x, 6 + i * (size + 6)), line, font=font, fill=PAPER,
                  stroke_width=2, stroke_fill=INK)
    return result


def _impact(main: str, width: int) -> list[tuple[Image.Image, int, int, float, str]]:
    """Two independently entering word blocks, thick offset ink and star rays."""
    main = main.strip()
    # Punctuation stays attached to its phrase, avoiding a floating comma.
    midpoint = max(1, len(main.rstrip("，,；;。！？!?")) // 2)
    words = [main[:midpoint], main[midpoint:]] if len(main) > 2 else [main]
    size = min(98, int((width - 90) / max(1, len(main)) * 1.05))
    images = [_outlined(word, size, YELLOW if i == 0 else PAPER,
                        tilt=5 if i == 0 else -4, shadow=CORAL) for i, word in enumerate(words)]
    total = sum(image.width for image in images) - 22 * (len(images) - 1)
    x = (width - total) // 2
    layers = []
    for i, image in enumerate(images):
        layers.append((image, x, 3 + 12 * i, .055 * i, "slam" if i == 0 else "slide"))
        x += image.width - 22
    return layers


def _bracket(main: str, width: int) -> Image.Image:
    word = _outlined(main, 82, PAPER, tilt=-3, shadow=CORAL, width=width - 72)
    image = Image.new("RGBA", (min(width, word.width + 76), word.height + 24))
    draw = ImageDraw.Draw(image)
    # Irregular marker swash, open brackets and short hand-drawn accent lines.
    baseline = image.height - 36
    draw.polygon([(22, baseline - 13), (image.width - 29, baseline - 27),
                  (image.width - 15, baseline + 3), (31, baseline + 12)], fill=CORAL)
    draw.line([(22, 25), (10, 29), (13, baseline - 1), (29, baseline + 3)], fill=YELLOW, width=5)
    draw.line([(image.width - 26, 20), (image.width - 10, 25),
               (image.width - 12, baseline - 8), (image.width - 26, baseline - 2)], fill=YELLOW, width=5)
    image.alpha_composite(word, ((image.width - word.width) // 2, 0))
    return image


def _stamp(main: str, detail: str, width: int) -> Image.Image:
    word = _outlined(main, 100, ORANGE, tilt=6, width=width // 2 + 35)
    tail = _outlined(detail, 48, PAPER, tilt=0, width=max(100, width - word.width - 22)) if detail else None
    total = word.width + (tail.width - 15 if tail else 0)
    image = Image.new("RGBA", (min(width, total + 18), max(word.height, 130) + 20))
    draw = ImageDraw.Draw(image)
    # Stamp corners remain open so this reads as editorial emphasis, not a UI pill.
    x1, y1, x2, y2 = 9, 22, image.width - 10, image.height - 20
    for line in ([(x1 + 22, y1), (x1, y1 + 2), (x1 + 1, y1 + 22)],
                 [(x2 - 22, y2), (x2, y2 - 2), (x2 - 1, y2 - 22)]):
        draw.line(line, fill=TEAL, width=7)
    image.alpha_composite(word, (2, 0))
    if tail:
        image.alpha_composite(tail, (word.width - 15, image.height - tail.height - 15))
    return image.rotate(-3, resample=Image.Resampling.BICUBIC, expand=True)


def _rays(width: int, height: int, color: str) -> Image.Image:
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    for points in ([(24, 49), (4, 31)], [(28, 70), (3, 66)],
                   [(width - 26, 46), (width - 4, 24)],
                   [(width - 27, 69), (width - 2, 65)]):
        draw.line(points, fill=INK, width=9)
        draw.line(points, fill=color, width=4)
    return image


def _sfx(path: Path, duration: float, cues: list[tuple[float, str]]) -> int:
    """Original click, soft pluck and short stamp; no crowd/laughter/music bed."""
    rate = 48000
    samples = array.array("h", [0]) * math.ceil(duration * rate)
    accepted: list[float] = []
    for cue, kind in sorted(cues):
        if cue < 0 or cue >= duration or (accepted and cue - accepted[-1] < .9) or len(accepted) >= 12:
            continue
        accepted.append(cue)
        seconds = {"impact": .14, "aside": .11, "stamp": .18}.get(kind, .12)
        start, count = round(cue * rate), round(seconds * rate)
        for index in range(min(count, len(samples) - start)):
            t, u = index / rate, index / count
            envelope = math.sin(math.pi * u) * math.exp(-4 * u)
            if kind == "aside":
                value = math.sin(2 * math.pi * (1100 * t - 1500 * t * t)) + .2 * math.sin(2 * math.pi * 2300 * t)
            elif kind == "stamp":
                value = .7 * math.sin(2 * math.pi * (180 * t - 150 * t * t)) + .2 * math.sin(2 * math.pi * 1450 * t)
            else:
                value = math.sin(2 * math.pi * 670 * t) + .35 * math.sin(2 * math.pi * 1480 * t)
            samples[start + index] = round(6300 * envelope * value)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())
    return len(accepted)


def _decorate(base: Path, output: Path, temp: Path, *, title: str,
              captions: list[tuple[float, str, bool]], offset: float,
              timings: list[ClipTiming] | None = None,
              visual_accents: list[tuple[float, str]] | None = None) -> dict:
    source = media._inspect(base)
    width, height, duration = source.width, source.height, source.duration
    portrait = height > width
    windows = _caption_windows(captions, offset, timings)
    # The lower quarter is deliberate: no inferred face tracking or arrows that
    # might label the wrong person. Preserve the entire original frame.
    safe_bottom = height - (160 if portrait else 90)
    area_width = width - (82 if portrait else 240)
    area_height = 235 if portrait else 205
    area_top = safe_bottom - area_height
    layers: list[tuple[Path, float, float, int, int, str]] = []

    def add(image: Image.Image, start: float, end: float, x: int, y: int, motion="none"):
        if min(end, duration) - start < .04:
            return
        path = temp / f"v2-layer-{len(layers):03}.png"
        image.save(path, "PNG")
        layers.append((path, start, min(end, duration), x, y, motion))

    sounds = list(visual_accents or [])
    if offset > .2 and not captions[0][2]:
        # Two separate beats; the original narration stays exactly where it was.
        word = _outlined("在场", 155 if portrait else 140, PAPER, tilt=4, shadow=CORAL)
        action = _outlined("开拍！", 110 if portrait else 94, YELLOW, tilt=-5, shadow=INK)
        # Portrait faces occupy the upper-middle of phone footage. Keep the
        # opening below that region too, not only the subsequent clip captions.
        intro_y = int(height * (.53 if portrait else .22))
        add(word, .05, offset - .10, (width - word.width) // 2 - 18, intro_y, "slam")
        action_start = min(.65, offset * .38)
        add(action, action_start, offset - .10, (width - action.width) // 2 + 25,
            intro_y + word.height - 30, "stamp")
        sparks = _rays(min(width - 60, 680), 160, YELLOW)
        add(sparks, action_start + .04, offset - .10, (width - sparks.width) // 2,
            intro_y + word.height - 7, "none")
        if title:
            theme = _literal(title, min(width - 150, 760), 120)
            add(theme, min(.28, offset * .2), offset - .10,
                (width - theme.width) // 2, 80 if portrait else intro_y - theme.height - 18, "slide")
        sounds.append((action_start, "impact"))
    elif offset == 0 and title and not captions[0][2]:
        # A title without narration still appears, briefly and outside faces.
        theme = _literal(title, min(width - 150, 760), 100)
        add(theme, .04, min(2.4, windows[0][1] - .12),
            (width - theme.width) // 2, 80, "soft")

    cues = plan_cues(captions, offset, timings)
    for cue in cues:
        x = (width - area_width) // 2
        if cue.style == "literal":
            layer = _literal(cue.main, area_width, area_height)
            add(layer, cue.start, cue.end, x, safe_bottom - layer.height, "soft")
            continue
        if cue.style == "impact":
            group = _impact(cue.main, area_width)
            main_height = max(y + image.height for image, _, y, _, _ in group)
            detail = _small(cue.detail, area_width - 70) if cue.detail else None
            total_height = main_height + (detail.height - 10 if detail else 0)
            top = max(area_top, safe_bottom - total_height)
            for image, dx, dy, delay, motion in group:
                add(image, cue.start + delay, cue.end, x + dx, top + dy, motion)
            if detail:
                add(detail, cue.start + .10, cue.end, (width - detail.width) // 2,
                    top + main_height - 10, "soft")
            rays = _rays(area_width, 130, YELLOW)
            add(rays, cue.start + .08, min(cue.start + .62, cue.end), x, top + 18)
        elif cue.style == "aside":
            layer = _bracket(cue.main, area_width)
            detail = _small(cue.detail, area_width - 70, size=30) if cue.detail else None
            top = max(area_top, safe_bottom - layer.height - (detail.height - 18 if detail else 0))
            if detail:
                add(detail, cue.start, cue.end, x + 32, top, "soft")
            add(layer, cue.emphasis, cue.end, (width - layer.width) // 2,
                top + (detail.height - 18 if detail else 0), "slide")
        else:
            layer = _stamp(cue.main, cue.detail, area_width - 12)
            # Rotation can add a few pixels; fit into the complete safe box.
            ratio = min(1, area_width / layer.width, area_height / layer.height)
            if ratio < 1:
                layer = layer.resize((round(layer.width * ratio), round(layer.height * ratio)), Image.Resampling.LANCZOS)
            add(layer, cue.start, cue.end, (width - layer.width) // 2,
                safe_bottom - layer.height, "stamp")
        # Aural emphasis on selected graphic beats, not on every hard cut.
        # Long sequences stay sparse; quiet literal captions have no cue.
        if cue.segment % 4 != 3:
            sounds.append((cue.emphasis, cue.style))

    # A small plain wordmark, no repeated score/clip counter or dual corner pills.
    brand = _small("在场", 112, size=23)
    for index, (_, _, protected) in enumerate(captions):
        cursor, end = windows[index]
        if not protected:
            add(brand, cursor, end, width - brand.width - 24, 21)

    args = ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2", "-filter_complex_threads", "1",
            "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(base)]
    filters = [f"[0:{source.video_index}]fps=30,setsar=1[v0]"]
    for index, (path, start, end, x, y, motion) in enumerate(layers, 1):
        length = end - start
        args += ["-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30",
                 "-t", f"{length:.6f}", "-i", str(path)]
        fade = min(.08, length / 4)
        effect = f",fade=t=out:st={max(0, length - fade):.6f}:d={fade:.6f}:alpha=1"
        if motion == "soft":
            effect = f",fade=t=in:st=0:d={fade:.6f}:alpha=1" + effect
        filters.append(f"[{index}:v]format=rgba{effect},setpts=PTS-STARTPTS+{start:.6f}/TB[l{index}]")
        elapsed = f"(t-{start:.6f})"
        # All displacement stays inside the spare 30 px of the safe lower zone.
        dx = f"+34*exp(-20*{elapsed})" if motion == "slide" else ""
        dy = {"slam": f"-24*exp(-15*{elapsed})*cos(23*{elapsed})",
              "stamp": f"-30*exp(-19*{elapsed})", "soft": f"+8*exp(-18*{elapsed})"}.get(motion, "")
        filters.append(f"[v{index - 1}][l{index}]overlay=x='{x}{dx}':y='{y}{dy}':"
                       f"eof_action=pass:repeatlast=0:enable='between(t,{start:.6f},{end:.6f})'[v{index}]")
    sound = temp / "v2-accents.wav"
    count = _sfx(sound, duration, sounds)
    args += ["-protocol_whitelist", "file,pipe", "-format_whitelist", "wav", "-i", str(sound)]
    audio_index = len(layers) + 1
    filters += [f"[0:{source.audio_index}]asplit=2[main][key]",
                f"[{audio_index}:a]aformat=sample_rates=48000:channel_layouts=stereo[fx]",
                "[fx][key]sidechaincompress=threshold=0.03:ratio=6:attack=5:release=80[quietfx]",
                "[main][quietfx]amix=inputs=2:duration=first:normalize=0,"
                "alimiter=limit=0.97:level=0:latency=1[a]"]
    args += ["-filter_complex", ";".join(filters), "-map", f"[v{len(layers)}]", "-map", "[a]",
             "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
             "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(output)]
    media._run("ffmpeg", args, timeout=90 + duration * 8, code="MEDIA_RENDER_FAILED")
    info = media.probe(output)
    if ((info["width"], info["height"]) != (width, height) or not info["has_audio"]
            or info["duration"] > 90.04 or abs(info["duration"] - duration) > .12):
        raise MediaError("MEDIA_RENDER_FAILED", "综艺包装的画幅、时长或音轨校验失败，未发布输出。")
    media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-threads", "2", "-protocol_whitelist", "file,pipe",
               "-format_whitelist", "mov", "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(output),
               "-f", "null", "-"], timeout=45 + duration * 2, code="MEDIA_RENDER_FAILED")
    return {**info, "packaging": "variety_v2", "renderer_revision": REVISION, "sound_accents": count,
            "cue_count": len(cues), "cue_summary": [
                {"segment": cue.segment, "style": cue.style, "start": round(cue.start, 3),
                 "emphasis": round(cue.emphasis, 3), "end": round(cue.end, 3)} for cue in cues]}


def render_variety(segments: list[dict], output: Path, title: str = "", narration: Path | None = None) -> dict:
    """Render an immutable new v2 output, retaining source timing and audio."""
    if not isinstance(output, Path) or output.suffix.lower() != ".mp4":
        raise MediaError("MEDIA_RENDER_FAILED", "综艺包装输出必须是本地 MP4 文件。")
    if output.exists() or output.is_symlink():
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标成片已存在，请创建新的版本。")
    _text(title, media.MAX_TITLE_LENGTH)
    if not isinstance(segments, list) or not 1 <= len(segments) <= media.MAX_SEGMENTS:
        raise MediaError("MEDIA_INVALID_SEGMENT", "请提供有效的本地片段。")
    for segment in segments:
        if not isinstance(segment, dict):
            raise MediaError("MEDIA_INVALID_SEGMENT", "片段参数无效。")
        _text(segment.get("caption", ""), media.MAX_CAPTION_LENGTH)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".variety-v2-", dir=output.parent) as directory:
            temp = Path(directory)
            from .editorial_plan import compile_editorial
            compile_editorial(segments)
            cut = temp / "cut.mp4"
            media.render([{**segment, "caption": ""} for segment in segments], cut, title="")
            captions = [(float(segment["end"]) - float(segment["start"]), segment.get("caption", ""),
                         segment.get("_preserve_expression") is True) for segment in segments]
            timings = timeline(segments)
            offset, base = 0.0, cut
            if narration is not None:
                pcm, rate, channels = _narration(narration)
                offset = math.ceil(len(pcm) / (rate * channels * 2) * 30 - 1e-8) / 30 + 2 / 30
                base = temp / "narrated.mp4"
                add_narrated_intro(cut, narration, base, title="")
            complete = temp / "complete.mp4"
            from .visual_beats import plan
            accents = [(at + .08, 'stamp' if beat.kind == 'cutout' else 'aside')
                       for beat, at in plan(segments, offset) if beat.kind in ('cutout', 'headline')]
            editorial=compile_editorial(segments,offset)
            accents += [(x['start']+.08,'stamp' if x['beat'].treatment=='chapter' else 'aside')
                        for x in editorial if x['beat'].treatment!='none']
            result = _decorate(base, complete, temp, title='' if editorial else title, captions=captions, offset=offset,
                               timings=timings, visual_accents=accents)
            if editorial:
                from .editorial_render import render_editorial
                complete,metadata=render_editorial(complete,temp/'editorial.mp4',segments,temp,offset)
                result.update(metadata)
            if any(s.get('visual_beats') for s in segments):
                from .visual_beats import decorate_visual_beats
                enhanced = temp / 'visual-beats.mp4'
                result.update(decorate_visual_beats(complete, enhanced, segments, temp, offset=offset))
                complete = enhanced
            from .automatic_treatment import render_automatic
            complete, automatic = render_automatic(complete, temp/'automatic.mp4', segments, temp, offset)
            result.update(automatic)
            os.link(complete, output)
            return result
    except MediaError:
        raise
    except FileExistsError:
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标成片已存在，请创建新的版本。") from None
    except (OSError, ValueError, TypeError, wave.Error):
        raise MediaError("MEDIA_RENDER_FAILED", "综艺包装失败，原片和已保存版本未改动。") from None
