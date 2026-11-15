"""Deterministic variety-show graphics and brief, locally synthesised accents.

This is an opt-in packaging pass, not another model or a new editorial decision.
It uses the approved literal captions. It never invents dialogue or reactions.
Unlike standard packaging, variety mixes quiet SFX and re-encodes audio; the
participant track remains at unity gain and keys a compressor on the SFX only.
"""

from __future__ import annotations

import array
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

from PIL import Image, ImageDraw, ImageFont

from . import media
from .editing import ClipTiming, timeline
from .media import MediaError
from .packaging import _narration, add_narrated_intro

CREAM = "#fff8ea"
INK = "#201c1a"
CORAL = "#ff778c"
YELLOW = "#ffda68"
MINT = "#9fe2cf"


def _font(size: int, text: str):
    # Use installed sans CJK fonts, never redistribute macOS system fonts.
    for candidate in (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if "DejaVu" in candidate and any(ord(c) > 0x2fff for c in text):
            continue
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    raise MediaError("MEDIA_FONT_UNAVAILABLE", "缺少中文黑体，暂时无法生成综艺花字。")


def _text(value: str, maximum: int) -> None:
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise MediaError("MEDIA_INVALID_TEXT", "花字内容过长或含无效字符，请缩短后重试。")


def _fit(text: str, width: int, size: int, lines: int):
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while size >= 14:
        font = _font(size, text)
        wrapped = media._wrap(text, draw, font, width)
        if len(wrapped) <= lines:
            return font, wrapped, size
        size -= 2
    raise MediaError("MEDIA_INVALID_TEXT", "花字无法完整放入安全区域，请缩短后重试。")


def _star(draw, x: float, y: float, radius: float, color: str):
    points = []
    for index in range(8):
        angle = index * math.pi / 4
        r = radius if index % 2 == 0 else radius * .32
        points.append((x + math.cos(angle) * r, y + math.sin(angle) * r))
    draw.polygon(points, fill=color)


def _caption(text: str, width: int, index: int) -> Image.Image:
    font, lines, size = _fit(text, width - 44, 48 if width < 800 else 52, 4)
    line_height = size + 14
    image = Image.new("RGBA", (width, len(lines) * line_height + 36))
    draw = ImageDraw.Draw(image)
    colors = (YELLOW, CREAM, MINT)
    for row, line in enumerate(lines):
        x = (width - draw.textlength(line, font=font)) / 2
        y = 9 + row * line_height
        draw.text((x + 3, y + 6), line, font=font, fill=INK, stroke_width=7, stroke_fill=INK)
        draw.text((x, y), line, font=font, fill=colors[index % 3], stroke_width=7, stroke_fill=CREAM)
        draw.text((x, y), line, font=font, fill=colors[index % 3], stroke_width=3, stroke_fill=INK)
    return image


def _pill(text: str, color: str, *, size=24, rotate=0) -> Image.Image:
    font = _font(size, text)
    width = math.ceil(font.getlength(text)) + 44
    image = Image.new("RGBA", (width + 16, size + 44))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((9, 12, width + 5, size + 37), radius=15, fill=INK)
    draw.rounded_rectangle((5, 5, width + 1, size + 30), radius=15, fill=color, outline=CREAM, width=3)
    draw.text((26, 13), text, font=font, fill=INK, stroke_width=1)
    return image.rotate(rotate, resample=Image.Resampling.BICUBIC, expand=True)


def _opening(width: int) -> Image.Image:
    image = Image.new("RGBA", (width, 345))
    draw = ImageDraw.Draw(image)
    font = _font(142, "在场")
    text_width = draw.textlength("在场", font=font)
    x = (width - text_width) / 2
    draw.text((x + 6, 21), "在场", font=font, fill=INK, stroke_width=10, stroke_fill=INK)
    draw.text((x, 12), "在场", font=font, fill=CORAL, stroke_width=10, stroke_fill=CREAM)
    draw.text((x, 12), "在场", font=font, fill=CORAL, stroke_width=4, stroke_fill=INK)
    label = _pill("开 拍 ！", YELLOW, size=57, rotate=-4)
    image.alpha_composite(label, ((width - label.width) // 2, 192))
    _star(draw, x - 28, 74, 24, YELLOW)
    _star(draw, x + text_width + 32, 154, 20, MINT)
    _star(draw, x - 2, 267, 13, CORAL)
    return image


def _theme(title: str, width: int) -> Image.Image:
    font, lines, size = _fit(title, width - 48, 28, 4)
    image = Image.new("RGBA", (width, len(lines) * (size + 10) + 32))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, width - 2, image.height - 2), radius=18, fill=INK)
    draw.rounded_rectangle((0, 0, width - 8, image.height - 8), radius=18, fill=YELLOW)
    for row, line in enumerate(lines):
        draw.text(((width - draw.textlength(line, font=font)) / 2 - 4, 8 + row * (size + 10)),
                  line, font=font, fill=INK, stroke_width=1)
    return image.rotate(2, resample=Image.Resampling.BICUBIC, expand=True)


def _sfx(path: Path, duration: float, cue_times: list[float]) -> int:
    """Create original, short accents; no downloaded or licensed third-party audio."""
    rate = 48000
    samples = array.array("h", [0]) * math.ceil(duration * rate)
    accepted = []
    for cue in cue_times:
        if cue < 0 or cue >= duration or (accepted and cue - accepted[-1] < .8) or len(accepted) >= 14:
            continue
        accepted.append(cue)
        start, count = round(cue * rate), round(.105 * rate)
        for index in range(min(count, len(samples) - start)):
            t, u = index / rate, index / count
            # Soft descending pop, measured peak about -25 dBFS before ducking.
            envelope = math.sin(math.pi * u) ** 2 * math.exp(-3 * u)
            value = (math.sin(2 * math.pi * (850 * t - 1700 * t * t))
                     + .22 * math.sin(2 * math.pi * 1560 * t))
            samples[start + index] = round(5300 * envelope * value)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples.tobytes())
    return len(accepted)


def _caption_windows(captions: list[tuple[float, str, bool]], offset: float,
                     timings: list[ClipTiming] | None = None) -> list[tuple[float, float]]:
    """Keep literal captions and their sound cues out of overlapping pictures."""
    if timings is None:
        timings = timeline([{"start": 0, "end": length} for length, _, _ in captions])
    if len(timings) != len(captions):
        raise ValueError("caption timeline mismatch")
    return [(offset + item.start + item.overlap,
             offset + item.end - (timings[index + 1].overlap if index + 1 < len(timings) else 0))
            for index, item in enumerate(timings)]


def _decorate(base: Path, output: Path, temp: Path, *, title: str,
              captions: list[tuple[float, str, bool]], offset: float,
              timings: list[ClipTiming] | None = None) -> dict:
    source = media._inspect(base)
    width, height, duration = source.width, source.height, source.duration
    portrait = height > width
    windows = _caption_windows(captions, offset, timings)
    layers: list[tuple[Path, float, float, int, int, bool]] = []

    def add(layer: Image.Image, start: float, end: float, x: int, y: int, animated=True):
        if end - start < .04:
            return
        path = temp / f"layer-{len(layers):02}.png"
        layer.save(path, "PNG")
        layers.append((path, start, min(end, duration), x, y, animated))

    badge = _pill("在场 · 本期放送", CREAM, size=20)
    if offset > 0 and not captions[0][2]:
        add(badge, 0, offset, 36, 34, False)
        opening = _opening(min(width - 60, 680))
        add(opening, 0, offset, (width - opening.width) // 2, int(height * (.27 if portrait else .20)))
        if title:
            theme = _theme(title, min(width - 120, 680))
            add(theme, .12, offset, (width - theme.width) // 2,
                min(height - theme.height - (160 if portrait else 55), int(height * .69)))
    elif offset == 0 and title and not captions[0][2]:
        theme = _theme(title, min(width - 120, 680))
        add(theme, 0, min(2.6, duration, windows[0][1]), (width - theme.width) // 2, 110)

    cues = [0.0] if offset >= .15 and not captions[0][2] else []
    for index, (_, caption, protected) in enumerate(captions):
        cursor, boundary = windows[index]
        end = min(boundary, duration)
        length = max(0, end - cursor)
        # Confirmed interviews/expressions already have permanent identity text.
        # Do not cover those labels or add tonal cues to their agreed expression.
        if protected:
            continue
        add(badge, cursor, end, 36, 34, False)
        marker = _pill(f"{index + 1:02} / {len(captions):02} · 片段", MINT if index % 2 else CORAL,
                       size=21, rotate=-3 if index % 2 else 3)
        add(marker, cursor, end, width - marker.width - 36, 34)
        if caption:
            layer = _caption(caption, width - 96 if portrait else min(960, width - 200), index)
            add(layer, cursor + min(.10, length / 8), min(end, cursor + max(3.8, len(caption) / 8)),
                (width - layer.width) // 2, height - layer.height - (155 if portrait else 90))
        if end - cursor >= .15:
            cues.append(cursor)

    args = ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2", "-filter_complex_threads", "1",
            "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(base)]
    filters = [f"[0:{source.video_index}]fps=30,setsar=1[v0]"]
    for index, (path, start, end, x, y, animated) in enumerate(layers, 1):
        length = end - start
        args += ["-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30",
                 "-t", f"{length:.6f}", "-i", str(path)]
        fade = min(.14, length / 4)
        effect = (f",fade=t=in:st=0:d={fade:.6f}:alpha=1,"
                  f"fade=t=out:st={max(0, length - fade):.6f}:d={fade:.6f}:alpha=1" if animated else "")
        filters.append(f"[{index}:v]format=rgba{effect},setpts=PTS-STARTPTS+{start:.6f}/TB[l{index}]")
        y_expr = (f"{y}+30*exp(-12*(t-{start:.6f}))*cos(18*(t-{start:.6f}))" if animated else str(y))
        filters.append(f"[v{index - 1}][l{index}]overlay=x={x}:y='{y_expr}':"
                       f"eof_action=pass:repeatlast=0:enable='between(t,{start:.6f},{end:.6f})'[v{index}]")
    sound = temp / "accents.wav"
    count = _sfx(sound, duration, cues)
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
    return {**info, "packaging": "variety", "sound_accents": count}


def render_variety(segments: list[dict], output: Path, title: str = "", narration: Path | None = None) -> dict:
    """Create a new packaged output while leaving every previous version intact."""
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
        with tempfile.TemporaryDirectory(prefix=".variety-", dir=output.parent) as directory:
            temp = Path(directory)
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
            result = _decorate(base, complete, temp, title=title, captions=captions, offset=offset, timings=timings)
            os.link(complete, output)
            return result
    except MediaError:
        raise
    except FileExistsError:
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标成片已存在，请创建新的版本。") from None
    except (OSError, ValueError, TypeError, wave.Error):
        raise MediaError("MEDIA_RENDER_FAILED", "综艺包装失败，原片和已保存版本未改动。") from None
