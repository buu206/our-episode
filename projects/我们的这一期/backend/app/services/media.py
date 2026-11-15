"""Local media validation and deterministic composition; no model or network calls.

The caller owns storage/authorization and supplies controlled local paths. ``probe``
checks metadata and decodes a sample frame; rendering decodes the selected spans.
Neither successful decoding nor rendering asserts that a story or caption is true.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

from .editing import timeline, validate_transitions


class MediaError(Exception):
    """An error safe to return to clients without exposing paths or stderr."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


MAX_SOURCE_BYTES = 1024**3
MAX_SOURCE_SECONDS = 600.0
MAX_OUTPUT_SECONDS = 90.0
MAX_EDGE = 4096
MAX_FPS = 60.0
MAX_SEGMENTS = 32
MAX_CAPTION_LENGTH = 400
MAX_TITLE_LENGTH = 120
_THREADS = "2"


@dataclass(frozen=True)
class _Source:
    path: Path
    duration: float
    width: int
    height: int
    video_index: int
    audio_index: int | None
    format: str

    def info(self) -> dict:
        return {
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "has_audio": self.audio_index is not None,
            "format": self.format,
        }


def _binary(name: str) -> str:
    candidate = shutil.which(name)
    if candidate:
        return candidate
    homebrew = Path("/opt/homebrew/bin") / name
    if homebrew.is_file():
        return str(homebrew)
    raise MediaError("MEDIA_TOOL_UNAVAILABLE", "本机媒体工具尚未就绪，请完成 FFmpeg 安装。")


def _run(name: str, args: list[str], *, timeout: float, code: str) -> bytes:
    # stderr is deliberately discarded: FFmpeg errors can contain source paths,
    # metadata and content. Only a fixed error message crosses this boundary.
    try:
        result = subprocess.run(
            [_binary(name), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise MediaError("MEDIA_TIMEOUT", "媒体处理超时，已停止本次处理。") from None
    except OSError:
        raise MediaError("MEDIA_TOOL_UNAVAILABLE", "无法运行本机媒体工具。") from None
    if result.returncode:
        messages = {
            "MEDIA_INVALID_FILE": "文件不是可读取的 MP4/MOV 视频。",
            "MEDIA_DECODE_FAILED": "视频轨道无法解码，请检查素材是否完整。",
            "MEDIA_RENDER_FAILED": "本地视频合成失败，原始素材未改动。",
        }
        raise MediaError(code, messages.get(code, "媒体处理失败。"))
    return result.stdout


def _number(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return number


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(str(stream.get(key, "0/0"))))
            if math.isfinite(value) and value > 0:
                return value
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
    return float("nan")


def _inspect(path: Path) -> _Source:
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise MediaError("MEDIA_INVALID_FILE", "素材必须是本地普通文件。")
        size = path.stat().st_size
        if size <= 0:
            raise MediaError("MEDIA_INVALID_FILE", "素材文件为空。")
        if size > MAX_SOURCE_BYTES:
            raise MediaError("MEDIA_LIMIT_EXCEEDED", "单个素材不得超过 1 GB。")
        path = path.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise MediaError("MEDIA_INVALID_FILE", "无法读取本地素材。") from None

    payload = _run(
        "ffprobe",
        [
            "-v", "error", "-max_alloc", "67108864",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-enable_drefs", "0", "-use_absolute_path", "0",
            "-probesize", "10000000", "-analyzeduration", "5000000",
            "-show_entries",
            "format=format_name,duration:format_tags=major_brand:"
            "stream=index,codec_type,codec_name,width,height,duration,avg_frame_rate,r_frame_rate:"
            "stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(path),
        ],
        timeout=25,
        code="MEDIA_INVALID_FILE",
    )
    try:
        metadata = json.loads(payload)
        container = metadata["format"]
        streams = metadata["streams"]
        if "mov" not in container.get("format_name", "").split(","):
            raise MediaError("MEDIA_UNSUPPORTED_CONTAINER", "仅支持真实的 MP4/MOV 视频容器。")
        brand = container.get("tags", {}).get("major_brand", "").strip().lower()
        allowed_brand = (
            not brand or brand == "qt" or brand.startswith("iso")
            or brand in {"mp41", "mp42", "avc1", "dash", "m4v", "msnv", "f4v"}
        )
        if not allowed_brand:
            raise MediaError("MEDIA_UNSUPPORTED_CONTAINER", "该媒体容器不在 MP4/MOV 试验范围内。")
        videos = [s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise MediaError("MEDIA_NO_VIDEO", "素材中没有可用的视频轨道。")
        video = videos[0]
        width, height = int(video["width"]), int(video["height"])
        duration = _number(video.get("duration", container.get("duration")))
        if not math.isfinite(duration) or duration <= 0:
            duration = _number(container.get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError("MEDIA_INVALID_FILE", "无法确定视频的有效时长。")
        fps = _fps(video)
        if width <= 0 or height <= 0:
            raise MediaError("MEDIA_INVALID_FILE", "无法确定视频画面尺寸。")
        if max(width, height) > MAX_EDGE or not math.isfinite(fps) or fps > MAX_FPS:
            raise MediaError("MEDIA_LIMIT_EXCEEDED", "试验素材最长边需不超过 4096 像素，帧率不超过 60 fps。")
        if duration > MAX_SOURCE_SECONDS + 0.01:
            raise MediaError("MEDIA_LIMIT_EXCEEDED", "单个素材时长不得超过 10 分钟。")
        rotation = _number(video.get("tags", {}).get("rotate", 0))
        for item in video.get("side_data_list", []):
            if "rotation" in item:
                rotation = _number(item["rotation"])
                break
        if not math.isfinite(rotation) or abs(rotation / 90 - round(rotation / 90)) > 0.001:
            raise MediaError("MEDIA_INVALID_FILE", "暂不支持该素材的旋转信息。")
        if round(rotation / 90) % 2:
            width, height = height, width
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        return _Source(
            path=path, duration=duration, width=width, height=height,
            video_index=int(video["index"]),
            audio_index=int(audio["index"]) if audio is not None else None,
            format="mov" if brand in {"", "qt"} else "mp4",
        )
    except (KeyError, TypeError, ValueError, IndexError):
        raise MediaError("MEDIA_INVALID_FILE", "无法读取有效的视频信息。") from None


def _decode_sample(source: _Source) -> None:
    raw = _run(
        "ffmpeg",
        [
            "-nostdin", "-v", "error", "-xerror", "-max_alloc", "67108864",
            "-threads", _THREADS, "-filter_threads", "1",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-enable_drefs", "0", "-use_absolute_path", "0",
            "-i", str(source.path), "-map", f"0:{source.video_index}",
            "-an", "-sn", "-dn", "-frames:v", "1",
            "-vf", "scale=32:32", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
        ],
        timeout=25,
        code="MEDIA_DECODE_FAILED",
    )
    if len(raw) != 32 * 32 * 3:
        raise MediaError("MEDIA_DECODE_FAILED", "未能解码出有效视频画面。")


def probe(path: Path) -> dict:
    """Inspect the real container and sample-decode a local video, without mutation."""
    source = _inspect(path)
    _decode_sample(source)
    return source.info()


def _font(size: int, text: str) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            if "DejaVu" in candidate and any(ord(c) > 0x2FFF for c in text):
                continue
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    if any(ord(c) > 127 for c in text):
        raise MediaError("MEDIA_FONT_UNAVAILABLE", "缺少中文字体，暂时无法生成中文文字图层。")
    return ImageFont.load_default(size=size)


def _wrap(text: str, draw: ImageDraw.ImageDraw, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for char in paragraph:
            if current and draw.textlength(current + char, font=font) > max_width:
                lines.append(current)
                current = char
            else:
                current += char
        lines.append(current)
    return lines


def _overlay(path: Path, width: int, height: int, caption: str, title: str) -> None:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for text, is_title in ((title, True), (caption, False)):
        if not text:
            continue
        size = 34 if width >= height else 30
        font = _font(size, text)
        lines = _wrap(text, draw, font, width - 96)
        while len(lines) > (3 if is_title else 5) and size > 18:
            size -= 2
            font = _font(size, text)
            lines = _wrap(text, draw, font, width - 96)
        if len(lines) > (3 if is_title else 5):
            raise MediaError("MEDIA_INVALID_TEXT", "文字过长，无法完整放入画面，请缩短后重试。")
        line_height = size + 12
        block_height = len(lines) * line_height + 24
        top = 30 if is_title else height - block_height - 32
        draw.rounded_rectangle((28, top, width - 28, top + block_height), radius=12, fill=(0, 0, 0, 185))
        for i, line in enumerate(lines):
            x = (width - draw.textlength(line, font=font)) / 2
            draw.text((x, top + 10 + i * line_height), line, font=font, fill="white", stroke_width=1)
    image.save(path, format="PNG")


def render(segments: list[dict], output: Path, title: str = "") -> dict:
    """Compose user-specified source spans; captions are rendered literally as PNG.

    Existing outputs and sources are never overwritten. The final file appears only
    after composition and validation succeed. This is deterministic local editing,
    not AI analysis or a claim that user-supplied text describes the source correctly.
    """
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise MediaError("MEDIA_INVALID_SEGMENT", "请提供 1 至 32 个有效片段。")
    if not isinstance(title, str) or len(title) > MAX_TITLE_LENGTH:
        raise MediaError("MEDIA_INVALID_TEXT", "标题不得超过 120 个字符。")
    try:
        validate_transitions(segments)
        timings = timeline(segments)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise MediaError("MEDIA_INVALID_SEGMENT", "转场类型、时长或相邻片段范围无效。") from None
    transitioning = any(item.overlap > 0 for item in timings)
    # A submitted interview is a confirmed expression. Never crossfade either
    # edge of it: that would replace part of the agreed audio with another clip.
    for index, timing in enumerate(timings):
        if timing.overlap and (segments[index].get("_preserve_expression") is True
                or segments[index - 1].get("_preserve_expression") is True):
            raise MediaError("MEDIA_INVALID_SEGMENT", "已确认采访前后只能直接切换，不能覆盖其声音。")

    prepared: list[tuple[_Source, float, float, str]] = []
    sources: dict[Path, _Source] = {}
    total = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise MediaError("MEDIA_INVALID_SEGMENT", "片段参数无效。")
        start, end = _number(segment.get("start")), _number(segment.get("end"))
        if (isinstance(segment.get("start"), bool) or isinstance(segment.get("end"), bool)
                or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
            raise MediaError("MEDIA_INVALID_SEGMENT", "片段起止时间必须有效，结束时间应大于开始时间。")
        caption = segment.get("caption", "")
        if not isinstance(caption, str) or len(caption) > MAX_CAPTION_LENGTH:
            raise MediaError("MEDIA_INVALID_TEXT", "单段文字不得超过 400 个字符。")
        if any(ord(c) < 32 and c not in "\n\t" for c in caption + title):
            raise MediaError("MEDIA_INVALID_TEXT", "文字不能包含不可显示的控制字符。")
        try:
            local_path = Path(segment["path"])
        except (KeyError, TypeError, ValueError):
            raise MediaError("MEDIA_INVALID_SEGMENT", "片段缺少有效的本地素材。") from None
        if local_path not in sources:
            sources[local_path] = _inspect(local_path)
            _decode_sample(sources[local_path])
        source = sources[local_path]
        if end > source.duration + 0.001:
            raise MediaError("MEDIA_INVALID_SEGMENT", "片段引用超出了原视频时长。")
        total = timings[len(prepared)].end
        if total > MAX_OUTPUT_SECONDS + 0.000001:
            raise MediaError("MEDIA_LIMIT_EXCEEDED", "成片总时长不得超过 90 秒。")
        prepared.append((source, start, end, caption))

    first = prepared[0][0]
    width, height = (1280, 720) if first.width >= first.height else (720, 1280)
    try:
        output = Path(output)
        if output.suffix.lower() != ".mp4":
            raise MediaError("MEDIA_RENDER_FAILED", "成片输出必须为 MP4 文件。")
        if output.exists() or output.is_symlink():
            raise MediaError("MEDIA_OUTPUT_EXISTS", "目标成片已存在，请创建新的版本。")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".render-", dir=output.parent) as temp_name:
            temp = Path(temp_name)
            clips = []
            for index, (source, start, end, caption) in enumerate(prepared):
                duration = timings[index].duration
                overlay = temp / f"overlay-{index:03}.png"
                clip = temp / f"clip-{index:03}.mp4"
                _overlay(overlay, width, height, caption, title if index == 0 else "")
                args = [
                    "-nostdin", "-v", "error", "-xerror", "-y",
                    "-max_alloc", "67108864", "-threads", _THREADS,
                    "-filter_complex_threads", "1", "-filter_threads", "1",
                    "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
                    "-enable_drefs", "0", "-use_absolute_path", "0",
                    "-ss", f"{start:.6f}", "-i", str(source.path),
                    "-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30",
                    "-i", str(overlay),
                ]
                if source.audio_index is None:
                    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                    audio_input = "2:a:0"
                else:
                    audio_input = f"0:{source.audio_index}"
                # Transition offsets use frame-aligned durations. Trim the
                # selected source *before* padding the final fraction of a frame
                # so rounding can never reveal content beyond the chosen end.
                video_trim = f"trim=duration={end-start:.9f},setpts=PTS-STARTPTS," if transitioning else ""
                video_pad = (f",tpad=stop_mode=clone:stop_duration=0.034,trim=duration={duration:.9f},"
                             "settb=expr=1/30,setpts=PTS-STARTPTS") if transitioning else ""
                audio_trim = f"atrim=duration={end-start:.9f},asetpts=PTS-STARTPTS," if transitioning else ""
                filters = (
                    f"[0:{source.video_index}]{video_trim}scale={width}:{height}:force_original_aspect_ratio=decrease:"
                    f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"setsar=1,fps=30,format=yuv420p{video_pad}[base];"
                    "[base][1:v:0]overlay=0:0:shortest=1,format=yuv420p[v];"
                    f"[{audio_input}]{audio_trim}aresample=48000,aformat=channel_layouts=stereo,apad,"
                    f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a]"
                )
                args += [
                    "-filter_complex", filters, "-map", "[v]", "-map", "[a]",
                    "-t", f"{duration:.6f}", "-map_metadata", "-1", "-map_chapters", "-1",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-threads", _THREADS, "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "128k", "-ar", "48000", "-ac", "2",
                    "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(clip),
                ]
                _run("ffmpeg", args, timeout=90 + duration * 8, code="MEDIA_RENDER_FAILED")
                clips.append(clip)

            complete = temp / "complete.mp4"
            if transitioning:
                _render_transitions(clips, segments, timings, complete)
            else:
                manifest = temp / "concat.txt"
                manifest.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
                _run(
                    "ffmpeg",
                    [
                        "-nostdin", "-v", "error", "-xerror", "-y",
                        "-threads", _THREADS, "-filter_threads", "1",
                        "-protocol_whitelist", "file,pipe", "-format_whitelist", "concat,mov",
                        "-f", "concat", "-safe", "1", "-i", str(manifest),
                        "-map", "0:v:0", "-map", "0:a:0", "-t", f"{total:.6f}",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", _THREADS,
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                        "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(complete),
                    ],
                    timeout=90 + total * 5,
                    code="MEDIA_RENDER_FAILED",
                )
            info = probe(complete)
            if (not info["has_audio"] or (info["width"], info["height"]) != (width, height)
                    or info["duration"] > MAX_OUTPUT_SECONDS + 0.04
                    or abs(info["duration"] - total) > 0.12):
                raise MediaError("MEDIA_RENDER_FAILED", "成片校验未通过，未发布本次结果。")
            # Same-filesystem link publishes a complete file atomically and refuses
            # concurrent overwrites, unlike os.replace. TemporaryDirectory removes
            # only this render's scratch files after the final link is established.
            os.link(complete, output)
            return info
    except FileExistsError:
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标成片已存在，请创建新的版本。") from None
    except (OSError, TypeError, ValueError):
        raise MediaError("MEDIA_RENDER_FAILED", "无法保存本地成片，原始素材未改动。") from None


def _render_transitions(clips, segments, timings, output: Path) -> None:
    """Join only locally-normalised clips using allowlisted deterministic filters.

    Hard cuts keep both complete sound spans; dissolves and fades overlap the
    selected ends with an equal-duration audio crossfade. No model filter text
    or source metadata is accepted as executable syntax.
    """
    args = ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", _THREADS,
            "-filter_complex_threads", "1", "-filter_threads", "1"]
    filters = []
    for index, clip in enumerate(clips):
        args += ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
                 "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(clip)]
        length = timings[index].duration
        filters += [f"[{index}:v:0]fps=30,trim=duration={length:.9f},settb=expr=1/30,setpts=PTS-STARTPTS[v{index}]",
                    f"[{index}:a:0]atrim=duration={length:.9f},asetpts=PTS-STARTPTS[a{index}]"]
    video, audio = "v0", "a0"
    for index in range(1, len(clips)):
        joined_video, joined_audio = f"jv{index}", f"ja{index}"
        overlap = timings[index].overlap
        if overlap:
            effect = {"dissolve": "dissolve", "fade_black": "fadeblack"}[segments[index]["transition"]]
            filters += [f"[{video}][v{index}]xfade=transition={effect}:duration={overlap:.9f}:"
                        f"offset={timings[index].start:.9f},settb=expr=1/30[{joined_video}]",
                        f"[{audio}][a{index}]acrossfade=d={overlap:.9f}:c1=tri:c2=tri[{joined_audio}]"]
        else:
            filters += [f"[{video}][v{index}]concat=n=2:v=1:a=0,settb=expr=1/30[{joined_video}]",
                        f"[{audio}][a{index}]concat=n=2:v=0:a=1[{joined_audio}]"]
        video, audio = joined_video, joined_audio
    total = timings[-1].end
    args += ["-filter_complex", ";".join(filters), "-map", f"[{video}]", "-map", f"[{audio}]",
             "-t", f"{total:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-threads", _THREADS, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-ar", "48000", "-ac", "2", "-map_metadata", "-1", "-map_chapters", "-1",
             "-movflags", "+faststart", str(output)]
    _run("ffmpeg", args, timeout=90 + total * 8, code="MEDIA_RENDER_FAILED")
