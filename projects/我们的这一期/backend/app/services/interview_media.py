"""Local interview validation and literal, permanently labelled media rendering.

The caller owns participant consent, revision confirmation, source ownership and
artifact registration. Recheck those before publishing a helper's output. This
module neither interprets speech nor claims the supplied expression is an ASR result.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import unicodedata

from PIL import Image, ImageDraw

from . import media
from .media import MediaError


MAX_INTERVIEW_BYTES = 100_000_000
MAX_INTERVIEW_SECONDS = 60.0
MAX_CONTEXT_SECONDS = 30.0
_AUDIO_FORMATS = {".wav": ("wav", "audio/wav"), ".mp3": ("mp3", "audio/mpeg"),
                  ".m4a": ("mov", "audio/mp4")}
_VIDEO_SUFFIXES = {".mp4", ".mov"}


def _path(path: Path, *, output=False) -> Path:
    try:
        if (not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
                or path.is_symlink() or any(parent.is_symlink() for parent in path.parents)):
            raise ValueError("uncontrolled path")
        if output:
            if path.exists():
                raise MediaError("MEDIA_OUTPUT_EXISTS", "目标文件已存在，请使用新的输出路径。")
            if path.suffix.lower() != ".mp4" or not path.parent.is_dir():
                raise ValueError("output must have a registered parent")
            return path
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("not a regular nonempty file")
        return path.resolve(strict=True)
    except (OSError, ValueError, TypeError):
        raise MediaError("MEDIA_INVALID_FILE", "媒体路径必须是受控的本地普通文件或新的MP4输出。") from None


def _input_options(container: str) -> list[str]:
    args = ["-protocol_whitelist", "file,pipe", "-format_whitelist", container]
    if container == "mov":
        args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    return args


def _metadata(path: Path, container: str) -> dict:
    raw = media._run("ffprobe", ["-v", "error", "-max_alloc", "67108864",
        *_input_options(container), "-probesize", "10000000", "-analyzeduration", "5000000",
        "-show_entries", "format=format_name,duration:format_tags=major_brand:"
        "stream=index,codec_type,codec_name,duration,sample_rate,channels:stream_disposition=attached_pic",
        "-of", "json", str(path)], timeout=25, code="MEDIA_INVALID_FILE")
    try:
        result = json.loads(raw)
        if container not in result["format"]["format_name"].split(","):
            raise ValueError("container mismatch")
        if not isinstance(result["streams"], list):
            raise ValueError("missing streams")
        return result
    except (ValueError, KeyError, TypeError):
        raise MediaError("MEDIA_INVALID_FILE", "媒体真实容器或轨道信息无效。") from None


def _audio_duration(path: Path, container: str, index: int) -> float:
    # Decode at most 61 seconds to bounded mono PCM. This removes MP3/AAC encoder
    # padding from the duration check without allocating unbounded output.
    pcm = media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-max_alloc", "67108864",
        "-threads", "2", "-filter_threads", "1", *_input_options(container), "-i", str(path),
        "-map", f"0:{index}", "-vn", "-sn", "-dn", "-t", "61", "-ar", "8000", "-ac", "1",
        "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"], timeout=90, code="MEDIA_DECODE_FAILED")
    if not pcm or len(pcm) % 2:
        raise MediaError("MEDIA_DECODE_FAILED", "采访音轨无法完整解码。")
    return len(pcm) / 16000


def _decode_complete(path: Path, container="mov", *, seconds=60.0) -> None:
    media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-max_alloc", "67108864",
        "-threads", "2", "-filter_threads", "1", *_input_options(container), "-i", str(path),
        "-map", "0:V:0?", "-map", "0:a:0?", "-sn", "-dn", "-f", "null", "-"],
        timeout=45 + seconds * 2, code="MEDIA_DECODE_FAILED")


def probe_interview(path: Path, kind: str = "audio") -> dict:
    """Return media.probe-compatible fields plus size and MIME; never return paths.

    Audio: genuine WAV/MP3/M4A with an audio track and no active video track.
    Video: genuine MP4/MOV. A silent video is inspectable but cannot be adopted
    through materialize_interview, which requires an original audio track.
    """
    if kind not in ("audio", "video"):
        raise MediaError("INTERVIEW_INVALID_KIND", "采访类型只能是audio或video。")
    path = _path(path)
    try:
        size = path.stat().st_size
    except OSError:
        raise MediaError("MEDIA_INVALID_FILE", "采访素材已不可用，请重新上传。") from None
    if size > MAX_INTERVIEW_BYTES:
        raise MediaError("INTERVIEW_LIMIT_EXCEEDED", "每份采访文件不得超过100MB。")
    suffix = path.suffix.lower()
    if kind == "video":
        if suffix not in _VIDEO_SUFFIXES:
            raise MediaError("MEDIA_INVALID_FILE", "视频采访仅支持真实的MP4或MOV。")
        info = media.probe(path)
        container = "mov"
        mime = "video/quicktime" if info["format"] == "mov" else "video/mp4"
    else:
        if suffix not in _AUDIO_FORMATS:
            raise MediaError("MEDIA_INVALID_FILE", "语音采访仅支持真实的WAV、MP3或M4A。")
        container, mime = _AUDIO_FORMATS[suffix]
        info = {"width": 0, "height": 0, "format": suffix[1:], "has_audio": True}
    metadata = _metadata(path, container)
    streams = metadata["streams"]
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    active_video = [s for s in streams if s.get("codec_type") == "video"
                    and not s.get("disposition", {}).get("attached_pic")]
    if kind == "audio" and (audio is None or active_video):
        raise MediaError("MEDIA_INVALID_FILE", "语音采访必须含有音轨且不能伪装成视频。")
    if kind == "audio" and suffix == ".mp3" and audio.get("codec_name") != "mp3":
        raise MediaError("MEDIA_INVALID_FILE", "文件不是真实的MP3音频。")
    if kind == "audio" and suffix == ".m4a":
        brand = metadata["format"].get("tags", {}).get("major_brand", "").strip().lower()
        if (audio.get("codec_name") not in {"aac", "alac"}
                or not (brand in {"m4a", "mp41", "mp42"} or brand.startswith("iso"))):
            raise MediaError("MEDIA_INVALID_FILE", "M4A采访需要有效的AAC或ALAC音轨。")
    declared = media._number(metadata["format"].get("duration"))
    if not math.isfinite(declared) or declared <= 0:
        raise MediaError("MEDIA_INVALID_FILE", "无法确定采访的有效时长。")
    # A small header allowance permits container/codec padding; decoded audio
    # and video durations below still enforce the actual sixty-second limit.
    if declared > MAX_INTERVIEW_SECONDS + .25:
        raise MediaError("INTERVIEW_LIMIT_EXCEEDED", "每份采访时长不得超过60秒。")
    audio_seconds = _audio_duration(path, container, int(audio["index"])) if audio else 0
    # AAC decoding may return a final padded frame beyond the container's edit
    # duration. Honour that bounded padding only; a substantially longer audio
    # stream must not evade the limit via a shorter declared duration.
    if (audio and audio.get("codec_name") == "aac"
            and 0 < audio_seconds - declared <= .05):
        audio_seconds = declared
    duration = audio_seconds if kind == "audio" else max(float(info["duration"]), audio_seconds)
    if not 0 < duration <= MAX_INTERVIEW_SECONDS + .001:
        raise MediaError("INTERVIEW_LIMIT_EXCEEDED", "每份采访时长不得超过60秒。")
    if kind == "video":
        _decode_complete(path, seconds=duration)
    return {**info, "duration": duration, "size": size, "media_type": mime}


def _span(start, end, duration: float, limit: float) -> tuple[float, float]:
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start or end > duration + .001):
        raise MediaError("MEDIA_INVALID_SEGMENT", "采用区间必须在来源内，且结束时间大于开始时间。")
    if end - start > limit + .000001:
        raise MediaError("MEDIA_LIMIT_EXCEEDED", f"本次片段不得超过{int(limit)}秒。")
    return float(start), float(end)


def _text(label, expression) -> tuple[str, str]:
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 40
            or len(label) > 40 or not isinstance(expression, str) or len(expression) > 100
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in label + expression)):
        raise MediaError("MEDIA_INVALID_TEXT", "采访称呼须为1–40字，表达说明最多100字，不能含控制字符。")
    return "事后采访 · " + label.strip(), ("本人确认的表达：" + expression if expression else "")


def _compose(source: Path, output: Path, start: float, end: float, *, kind: str,
             identity="", expression="") -> dict:
    output = _path(output, output=True)
    duration = end - start
    container = _AUDIO_FORMATS[source.suffix.lower()][0] if kind == "audio" else "mov"
    metadata = _metadata(source, container)
    audio = next((s for s in metadata["streams"] if s.get("codec_type") == "audio"), None)
    try:
        with tempfile.TemporaryDirectory(prefix=".interview-", dir=output.parent) as directory:
            temp = Path(directory)
            overlay, complete = temp / "identity.png", temp / "complete.mp4"
            args = ["-nostdin", "-v", "error", "-xerror", "-y", "-max_alloc", "67108864",
                    "-threads", "2", "-filter_threads", "1", "-filter_complex_threads", "1"]
            if kind == "audio":
                card = temp / "card.png"
                media._overlay(overlay, 1280, 720, expression, identity)
                canvas = Image.new("RGBA", (1280, 720), (244, 241, 233, 255))
                draw = ImageDraw.Draw(canvas)
                title = "事后补充 · 原声采访"
                font = media._font(48, title)
                draw.text(((1280 - draw.textlength(title, font=font)) / 2, 320), title,
                          font=font, fill=(41, 40, 35, 255))
                with Image.open(overlay) as layer:
                    Image.alpha_composite(canvas, layer.convert("RGBA")).convert("RGB").save(card, "PNG")
                args += ["-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30", "-i", str(card),
                         *_input_options(container), "-ss", f"{start:.9f}", "-i", str(source)]
                video_filter = "[0:v:0]setsar=1,fps=30,format=yuv420p[v];"
                audio_input = f"1:{audio['index']}"
            else:
                args += [*_input_options("mov"), "-ss", f"{start:.9f}", "-i", str(source)]
                video_filter = ("[0:V:0]scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,"
                    "setpts=PTS-STARTPTS,format=yuv420p")
                if identity:
                    media._overlay(overlay, 1280, 720, expression, identity)
                    args += ["-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30", "-i", str(overlay)]
                    video_filter += "[base];[base][1:v:0]overlay=0:0:shortest=1,format=yuv420p[v];"
                else:
                    video_filter += "[v];"
                if audio:
                    audio_input = f"0:{audio['index']}"
                else:
                    # Context clips may be silent; no human voice is fabricated.
                    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                    audio_input = "1:a:0"
            filters = video_filter + (f"[{audio_input}]aresample=48000,aformat=channel_layouts=stereo,"
                f"apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a]")
            args += ["-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-t", f"{duration:.9f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-map_chapters", "-1", "-metadata:s:v:0", "rotate=0",
                "-movflags", "+faststart", str(complete)]
            media._run("ffmpeg", args, timeout=90 + duration * 8, code="MEDIA_RENDER_FAILED")
            info = media.probe(complete)
            rendered = _metadata(complete, "mov")
            audio_spec = next((s for s in rendered["streams"] if s.get("codec_type") == "audio"), {})
            if (not info["has_audio"] or (info["width"], info["height"]) != (1280, 720)
                    or abs(info["duration"] - duration) > .08
                    or audio_spec.get("codec_name") != "aac" or audio_spec.get("channels") != 2
                    or audio_spec.get("sample_rate") != "48000"):
                raise MediaError("MEDIA_RENDER_FAILED", "采访成片时长、画面或音轨验证失败。")
            _decode_complete(complete, seconds=info["duration"])
            os.link(complete, output)
            return {**info, "size": output.stat().st_size, "media_type": "video/mp4"}
    except MediaError:
        raise
    except FileExistsError:
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标文件已存在，请使用新的输出路径。") from None
    except (OSError, ValueError, TypeError):
        raise MediaError("MEDIA_RENDER_FAILED", "本地采访合成失败，原始素材未改动。") from None


def materialize_interview(source: Path, kind: str, output: Path, label: str,
                          start: float = 0, end: float | None = None, expression: str = "") -> dict:
    """Burn identity and participant-confirmed expression into every video frame.

    ``expression`` is the participant's confirmed text, never automatic speech
    recognition. Original audio is transcoded without added speech or music.
    """
    identity, caption = _text(label, expression)
    source = _path(source)
    info = probe_interview(source, kind)
    if not info["has_audio"]:
        raise MediaError("INTERVIEW_AUDIO_REQUIRED", "采访没有原声音轨，不能作为有声采访采用。")
    start, end = _span(start, info["duration"] if end is None else end, info["duration"], 60)
    return _compose(source, output, start, end, kind=kind, identity=identity, expression=caption)


def context_clip(source: Path, output: Path, start: float, end: float) -> dict:
    """Re-encode only the caller-authorised <=30 s span; strip source metadata.

    The ordinary source may be longer than an interview (up to media's 10 min/
    1 GB limits). No whole source is staged, copied or returned to the recipient.
    """
    source = _path(source)
    if source.suffix.lower() not in _VIDEO_SUFFIXES:
        raise MediaError("MEDIA_INVALID_FILE", "上下文片段只接受本地MP4或MOV来源。")
    info = media.probe(source)
    start, end = _span(start, end, info["duration"], MAX_CONTEXT_SECONDS)
    return _compose(source, output, start, end, kind="video")


def preview_contribution(source: Path, output: Path) -> dict:
    """Normalise a complete <=600 s/1 GiB contribution for its owner's browser.

    This is ordinary contributed video, not an interview: no interview identity
    or expression text is added. Silent input receives only a compatibility
    silence track. The caller keeps the unchanged original for later adoption
    and enforces the remaining shared episode quota separately.
    """
    source = _path(source)
    if source.suffix.lower() not in _VIDEO_SUFFIXES:
        raise MediaError("MEDIA_INVALID_FILE", "普通供材预览只接受本地MP4或MOV来源。")
    info = media.probe(source)
    return _compose(source, output, 0.0, info["duration"], kind="video")
