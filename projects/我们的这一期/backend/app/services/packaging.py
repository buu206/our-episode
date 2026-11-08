"""Add a separate narrated opening; never mix narration into participant audio."""

from __future__ import annotations

import array
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import wave

from PIL import Image

from . import media
from .media import MediaError


def _narration(path: Path) -> tuple[bytes, int, int]:
    """Validate PCM and trim only leading/trailing low-level silence.

    Keep 80 ms of context on each side. The -54 dBFS threshold is deliberately
    low; it is a signal-level check and does not claim to recognise speech.
    """
    try:
        if not isinstance(path, Path) or path.is_symlink() or not path.is_file() or path.stat().st_size > 6_000_000:
            raise ValueError("invalid local WAV")
        with wave.open(str(path.resolve()), "rb") as audio:
            rate, channels, width, frames = audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()
            if (rate not in (8000, 16000, 22050, 24000, 44100, 48000) or channels not in (1, 2)
                    or width != 2 or audio.getcomptype() != "NONE" or not 0 < frames / rate <= 30):
                raise ValueError("unsupported PCM")
            pcm = audio.readframes(frames)
            if len(pcm) != frames * channels * width:
                raise ValueError("truncated PCM")
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        active = (index // channels for index, value in enumerate(samples) if abs(value) > 65)
        first = next(active, None)
        if first is None:
            raise MediaError("NARRATION_SILENT", "旁白只有静音，无法添加有声片头。")
        last = first
        for last in active:
            pass
        start, end = max(0, first - round(rate * 0.08)), min(frames, last + 1 + round(rate * 0.08))
        return pcm[start * channels * 2:end * channels * 2], rate, channels
    except MediaError:
        raise
    except (OSError, ValueError, wave.Error, EOFError):
        raise MediaError("INVALID_NARRATION", "旁白必须是完整可解码且不超过30秒的本地PCM WAV。") from None


def _audio_spec(path: Path) -> dict:
    raw = media._run("ffprobe", ["-v", "error", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov", "-enable_drefs", "0", "-use_absolute_path", "0",
        "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels",
        "-of", "json", str(path)], timeout=20, code="MEDIA_INVALID_FILE")
    try:
        return json.loads(raw)["streams"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise MediaError("PACKAGING_AUDIO_REQUIRED", "原片缺少可保留的AAC音轨。") from None


def add_narrated_intro(cut: Path, narration: Path, output: Path, title: str = "") -> dict:
    """Prefix an existing 720p cut with its first frame, literal title and narration.

    Output paths must be new version paths (UUIDs are assigned by the caller).
    Participant AAC packets are stream-copied without mixing, volume changes or
    re-encoding. The source video and narration are never overwritten.
    """
    if not isinstance(cut, Path) or not isinstance(output, Path):
        raise MediaError("MEDIA_INVALID_FILE", "包装需要受控本地路径。")
    if (not isinstance(title, str) or len(title) > media.MAX_TITLE_LENGTH
            or any(ord(char) < 32 and char not in "\n\t" for char in title)):
        raise MediaError("MEDIA_INVALID_TEXT", "片头标题格式无效或超过120字。")
    if output.exists() or output.is_symlink():
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标版本已存在，请使用新的输出文件。")
    if output.suffix.lower() != ".mp4":
        raise MediaError("MEDIA_RENDER_FAILED", "包装输出必须是MP4文件。")
    source = media._inspect(cut)
    media._decode_sample(source)
    if source.audio_index is None:
        raise MediaError("PACKAGING_AUDIO_REQUIRED", "原片没有音轨，不能声明保留其原声。")
    if (source.width, source.height) not in ((1280, 720), (720, 1280)):
        raise MediaError("PACKAGING_FORMAT_REQUIRED", "请先将真人片段合成为720p版本，再添加片头。")
    spec = _audio_spec(source.path)
    if spec.get("codec_name") != "aac" or spec.get("sample_rate") != "48000" or spec.get("channels") != 2:
        raise MediaError("PACKAGING_FORMAT_REQUIRED", "真人片段需使用既有合成器的48kHz双声道AAC原声音轨。")
    pcm, rate, channels = _narration(narration)
    voice_duration = len(pcm) / (rate * channels * 2)
    intro_duration = math.ceil(voice_duration * 30 - 1e-8) / 30
    # Two video frames provide a bounded splice allowance for AAC encoder delay.
    # Without it, the cut's negative priming timestamp can overlap the intro's
    # final packet. Keeping that original packet is necessary for exact audio
    # preservation. 80 ms trim context + frame rounding + allowance stays <300 ms.
    cut_offset = intro_duration + 2 / 30
    if source.duration + cut_offset > 90:
        raise MediaError("MEDIA_LIMIT_EXCEEDED", "片头加真人片段超过90秒，请缩短旁白或剪辑后另存版本。")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".packaging-", dir=output.parent) as directory:
            temp = Path(directory)
            trimmed = temp / "narration.wav"
            with wave.open(str(trimmed), "wb") as audio:
                audio.setnchannels(channels)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(pcm)
            first, overlay, card = temp / "first.png", temp / "title.png", temp / "card.png"
            media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2",
                "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
                "-enable_drefs", "0", "-use_absolute_path", "0", "-i", str(source.path),
                "-map", f"0:{source.video_index}", "-an", "-sn", "-dn", "-frames:v", "1", str(first)],
                timeout=30, code="MEDIA_RENDER_FAILED")
            media._overlay(overlay, source.width, source.height, "", title)
            with Image.open(first) as frame, Image.open(overlay) as layer:
                Image.alpha_composite(frame.convert("RGBA"), layer.convert("RGBA")).convert("RGB").save(card, "PNG")
            intro = temp / "intro.mp4"
            media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2",
                "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-loop", "1", "-framerate", "30",
                "-i", str(card), "-protocol_whitelist", "file,pipe", "-format_whitelist", "wav", "-i", str(trimmed),
                "-map", "0:v:0", "-map", "1:a:0", "-vf", "setsar=1,fps=30,format=yuv420p",
                "-af", f"aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={intro_duration:.9f}",
                "-t", f"{intro_duration:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(intro)],
                timeout=90, code="MEDIA_RENDER_FAILED")
            # Internal constant names avoid putting source filenames in concat syntax.
            staged_cut = temp / "cut.mp4"
            shutil.copyfile(source.path, staged_cut)
            manifest = temp / "concat.txt"
            manifest.write_text(f"file 'intro.mp4'\nduration {cut_offset:.9f}\nfile 'cut.mp4'\n", encoding="utf-8")
            complete = temp / "complete.mp4"
            media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2",
                "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "concat,mov",
                "-f", "concat", "-safe", "1", "-i", str(manifest), "-map", "0:v:0", "-map", "0:a:0",
                "-vf", "fps=30,setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
                "-movflags", "+faststart", str(complete)], timeout=90 + source.duration * 5, code="MEDIA_RENDER_FAILED")
            info = media.probe(complete)
            if (not info["has_audio"] or (info["width"], info["height"]) != (source.width, source.height)
                    or info["duration"] > 90 or abs(info["duration"] - (source.duration + cut_offset)) > 0.12):
                raise MediaError("MEDIA_RENDER_FAILED", "包装结果时长或音轨校验失败，未发布输出。")
            # Decode all streams, not only the sample frame, before publication.
            media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-threads", "2",
                "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-enable_drefs", "0",
                "-use_absolute_path", "0", "-i", str(complete), "-f", "null", "-"],
                timeout=45 + info["duration"] * 2, code="MEDIA_RENDER_FAILED")
            os.link(complete, output)
            return info
    except MediaError:
        raise
    except FileExistsError:
        raise MediaError("MEDIA_OUTPUT_EXISTS", "目标版本已存在，请使用新的输出文件。") from None
    except (OSError, TypeError, ValueError, wave.Error):
        raise MediaError("MEDIA_RENDER_FAILED", "本地片头合成失败，源文件未改动。") from None
