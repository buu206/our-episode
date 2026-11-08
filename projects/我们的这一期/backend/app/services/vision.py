"""Local sparse-frame preparation and guarded, unverified visual observations.

No environment files are read. Preparation is local and never writes extracted
frames. The caller must recheck ownership/revocation and persist the returned
observations before human review; they are not verified director evidence.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import media
from .director import CloudDirector, DirectorError, _invalid_constant, _unique_object

VisionError = DirectorError
MAX_ASSETS = 8
MAX_FRAMES = 48
FRAMES_PER_GROUP = 16
SAMPLE_WINDOW_SECONDS = 15.0
MAX_JPEG_BYTES = 512_000
RESERVATION_PER_GROUP = Decimal("0.85")
_UUID = re.compile(r"(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\Z")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Gap(_Strict):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class Coverage(_Strict):
    asset_id: str
    duration: float = Field(gt=0, le=600, allow_inf_nan=False)
    sample_times: list[float]
    unsampled_intervals: list[Gap]
    max_gap_seconds: float
    method: Literal["uniform_snapshots"] = "uniform_snapshots"
    audio_analyzed: Literal[False] = False
    time_basis: Literal["nominal_seek_seconds"] = "nominal_seek_seconds"


class VisionObservation(_Strict):
    asset_id: str = Field(min_length=1, max_length=128)
    time: float = Field(ge=0, allow_inf_nan=False)
    frame_id: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=1000)
    verification: Literal["unverified"] = "unverified"

    @field_validator("note")
    @classmethod
    def meaningful_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty observation")
        return value


class _GroupResult(_Strict):
    observations: list[VisionObservation] = Field(max_length=32)
    uncertainties: list[str] = Field(max_length=32)

    @field_validator("uncertainties")
    @classmethod
    def meaningful_uncertainties(cls, values: list[str]) -> list[str]:
        if any(not text.strip() or len(text) > 1000 for text in values):
            raise ValueError("invalid uncertainty")
        return values


class VisionResult(_GroupResult):
    observations: list[VisionObservation] = Field(max_length=96)
    uncertainties: list[str] = Field(max_length=110)
    coverage: list[Coverage]
    verification: Literal["unverified"] = "unverified"


@dataclass(frozen=True)
class FrameSample:
    asset_id: str
    time: float
    frame_id: str
    jpeg: bytes = field(repr=False)


@dataclass(frozen=True)
class PreparedVideo:
    frames: tuple[FrameSample, ...] = field(repr=False)
    coverage: tuple[Coverage, ...]

    @property
    def required_reservation_yuan(self) -> float:
        return float(RESERVATION_PER_GROUP * math.ceil(len(self.frames) / FRAMES_PER_GROUP))

    @property
    def request_digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps(self.manifest(), sort_keys=True, separators=(",", ":")).encode())
        for frame in self.frames:
            digest.update(hashlib.sha256(frame.jpeg).digest())
        return digest.hexdigest()

    def manifest(self) -> dict:
        """Metadata only: safe to persist; contains neither paths nor image data."""
        return {"coverage": [item.model_dump() for item in self.coverage],
                "frames": [{"asset_id": f.asset_id, "time": f.time, "frame_id": f.frame_id}
                           for f in self.frames],
                "groups": math.ceil(len(self.frames) / FRAMES_PER_GROUP)}


def _coverage(asset_id: str, duration: float) -> Coverage:
    count = max(1, math.ceil(duration / SAMPLE_WINDOW_SECONDS))
    times = [round(duration * (index + 0.5) / count, 6) for index in range(count)]
    bounds = [0.0, *times, duration]
    gaps = [Gap(start=start, end=end) for start, end in zip(bounds, bounds[1:]) if end > start]
    return Coverage(asset_id=asset_id, duration=duration, sample_times=times,
                    unsampled_intervals=gaps, max_gap_seconds=max(g.end - g.start for g in gaps))


def _controlled_path(raw: Any, root: Path) -> Path:
    if (not isinstance(raw, Path) or not raw.is_absolute() or raw.parent != root
            or raw.is_symlink() or not _UUID.fullmatch(raw.stem)
            or raw.suffix.lower() not in (".mp4", ".mov")):
        raise VisionError("INVALID_LOCAL_ASSET", "抽帧只接受应用素材目录中的 UUID 视频副本。")
    return raw


def _jpeg_frame(path: Path, seconds: float) -> bytes:
    # No frame images are written to disk; no metadata is carried into JPEG.
    return media._run("ffmpeg", [
        "-nostdin", "-v", "error", "-xerror", "-max_alloc", "67108864",
        "-threads", "2", "-filter_threads", "1",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
        "-enable_drefs", "0", "-use_absolute_path", "0",
        "-ss", f"{seconds:.6f}", "-i", str(path), "-map", "0:V:0",
        "-an", "-sn", "-dn", "-frames:v", "1",
        "-vf", "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-map_metadata", "-1", "-threads", "2", "-q:v", "4",
        "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1",
    ], timeout=30, code="MEDIA_DECODE_FAILED")


def _check_jpeg(data: bytes) -> None:
    try:
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_JPEG_BYTES:
            raise ValueError("image limit")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.size != (512, 512):
                raise ValueError("image format")
            image.verify()
    except (OSError, ValueError, TypeError):
        raise VisionError("INVALID_FRAME", "抽帧图片格式或大小无效。") from None


def prepare_video(assets: list[dict], *, media_root: Path) -> PreparedVideo:
    """Sample application-owned copies; caller checks ownership before entry.

    One midpoint per <=15-second bin, at least one frame per short clip. This is
    sparse image inspection, not continuous video/audio understanding. Times are
    requested seek positions; compressed-media frame boundaries may differ.
    """
    try:
        if not isinstance(media_root, Path) or media_root.is_symlink():
            raise ValueError("invalid root")
        root = media_root.resolve(strict=True)
        if not root.is_dir() or root.name != "assets":
            raise ValueError("invalid root")
    except (OSError, ValueError, TypeError):
        raise VisionError("INVALID_LOCAL_ASSET", "本地素材目录无效。") from None
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_ASSETS:
        raise VisionError("INVALID_ASSETS", "每期需要 1–8 个有效本地素材。")
    sources = []
    total_duration = total_size = 0
    identifiers, paths = set(), set()
    for asset in assets:
        if (not isinstance(asset, dict) or set(asset) != {"asset_id", "path"}
                or not isinstance(asset["asset_id"], str) or not _UUID.fullmatch(asset["asset_id"])
                or asset["asset_id"] in identifiers):
            raise VisionError("INVALID_ASSETS", "素材标识无效或重复。")
        path = _controlled_path(asset["path"], root)
        if path in paths:
            raise VisionError("INVALID_ASSETS", "素材副本不能重复。")
        info = media.probe(path)
        duration = info["duration"]
        total_duration += duration
        try:
            total_size += path.stat().st_size
        except OSError:
            raise VisionError("INVALID_LOCAL_ASSET", "素材副本已不可用。") from None
        if total_duration > 600 or total_size > 1024**3:
            raise VisionError("MEDIA_LIMIT_EXCEEDED", "每期素材总时长不得超过 10 分钟、总大小不得超过 1 GB。")
        coverage = _coverage(asset["asset_id"], duration)
        sources.append((path, coverage))
        identifiers.add(asset["asset_id"])
        paths.add(path)
    if sum(len(c.sample_times) for _, c in sources) > MAX_FRAMES:
        raise VisionError("FRAME_LIMIT_EXCEEDED", "本轮抽帧数量超过上限。")
    frames = []
    for path, coverage in sources:
        for index, seconds in enumerate(coverage.sample_times):
            _controlled_path(path, root)
            data = _jpeg_frame(path, seconds)
            _check_jpeg(data)
            frames.append(FrameSample(coverage.asset_id, seconds,
                                      f"{coverage.asset_id}_f{index + 1:03d}", data))
    return PreparedVideo(tuple(frames), tuple(c for _, c in sources))


def _validate_prepared(prepared: PreparedVideo) -> None:
    try:
        if (not isinstance(prepared, PreparedVideo) or not 1 <= len(prepared.frames) <= MAX_FRAMES
                or not 1 <= len(prepared.coverage) <= MAX_ASSETS
                or sum(c.duration for c in prepared.coverage) > 600):
            raise ValueError("invalid packet")
        expected = {}
        for coverage in prepared.coverage:
            if not _UUID.fullmatch(coverage.asset_id) or coverage != _coverage(coverage.asset_id, coverage.duration):
                raise ValueError("invalid coverage")
            for index, seconds in enumerate(coverage.sample_times):
                frame_id = f"{coverage.asset_id}_f{index + 1:03d}"
                if frame_id in expected:
                    raise ValueError("duplicate coverage")
                expected[frame_id] = (coverage.asset_id, seconds)
        if len(expected) != len(prepared.frames):
            raise ValueError("mismatched count")
        for frame in prepared.frames:
            if expected.pop(frame.frame_id, None) != (frame.asset_id, frame.time):
                raise ValueError("mismatched frame")
            _check_jpeg(frame.jpeg)
    except (ValueError, TypeError, AttributeError):
        raise VisionError("INVALID_PREPARED_VIDEO", "本地抽帧材料校验失败。") from None


def parse_observations(content: str, frames: tuple[FrameSample, ...]) -> _GroupResult:
    try:
        if not isinstance(content, str) or len(content.encode()) > 100_000:
            raise ValueError("response size")
        data = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        result = _GroupResult.model_validate(data)
        if not result.observations and not result.uncertainties:
            raise ValueError("no observation or reason")
        sources = {frame.frame_id: frame for frame in frames}
        seen = set()
        for observation in result.observations:
            source = sources.get(observation.frame_id)
            if (source is None or observation.frame_id in seen
                    or source.asset_id != observation.asset_id or source.time != observation.time):
                raise ValueError("unseen frame or time")
            seen.add(observation.frame_id)
        return result
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise VisionError("INVALID_MODEL_OUTPUT", "视觉结果未通过结构或来源校验。") from None


def _aggregate(requests: list[dict]) -> dict:
    known = bool(requests) and all(item.get("cost_status") == "known" for item in requests)
    return {"requests": list(requests), "model": "qwen3-vl-plus", "retries": 0,
            "input_tokens": sum(item["input_tokens"] for item in requests) if known else None,
            "output_tokens": sum(item["output_tokens"] for item in requests) if known else None,
            "cost_yuan": float(sum(Decimal(str(item["cost_yuan"])) for item in requests)) if known else (0.0 if not requests else None),
            "cost_status": "known" if known else ("unknown" if requests else "not_sent")}


class CloudVision(CloudDirector):
    """Sparse image adapter, disabled unless all inherited guards are satisfied.

    budget_check(reservation, required_yuan, request_digest) must verify a durable
    reservation bound to this exact packet, owner/task and the shared 3/30 yuan
    ledgers. It is called before each group; True must be a bool. This adapter does
    not settle/release reservations. Unknown results remain occupied. No automatic
    retries: a new attempt needs another persisted permission in the same ledger.
    """

    max_output_tokens = 2048
    required_reservation_yuan = 2.55  # maximum; actual value is on PreparedVideo

    def ready(self) -> tuple[bool, str]:
        try:
            self._guard()
            return True, "视觉抽帧接口配置就绪；仍需逐批预算许可和人工核对。"
        except DirectorError as exc:
            return False, exc.message

    async def _permit(self, reservation: Any, prepared: PreparedVideo, usage: dict) -> None:
        self._guard()
        try:
            if self._budget_check is None or reservation is None:
                raise ValueError("missing permission")
            allowed = self._budget_check(reservation, prepared.required_reservation_yuan, prepared.request_digest)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if allowed is not True:
                raise ValueError("permission denied")
        except Exception:
            raise VisionError("BUDGET_PERMISSION_REQUIRED", "视觉分析需要绑定本批素材的持久预算预留许可。", usage=usage) from None

    async def analyze(self, prepared: PreparedVideo, *, reservation: Any = None) -> dict:
        self._guard()
        _validate_prepared(prepared)
        await self._permit(reservation, prepared, _aggregate([]))
        try:
            prompt = Path(__file__).with_name("prompts").joinpath("vision.txt").read_text(encoding="utf-8")
            client = (self._client_factory() if self._client_factory else
                      httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=False, trust_env=False))
        except Exception:
            raise VisionError("CLIENT_INIT_FAILED", "视觉客户端初始化失败，尚未发送请求。", usage=_aggregate([])) from None
        requests, observations, uncertainties = [], [], []
        try:
            async with client:
                for offset in range(0, len(prepared.frames), FRAMES_PER_GROUP):
                    await self._permit(reservation, prepared, _aggregate(requests))
                    frames = prepared.frames[offset:offset + FRAMES_PER_GROUP]
                    content = [{"type": "text", "text": prompt}]
                    for frame in frames:
                        content.append({"type": "text", "text": json.dumps({"frame_id": frame.frame_id,
                                        "asset_id": frame.asset_id, "time": frame.time}, separators=(",", ":"))})
                        content.append({"type": "image_url", "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode("ascii")}})
                    requests.append(self._usage(None, None, 0, True))
                    try:
                        response = await client.post(self._base_url + "/chat/completions",
                            headers={"Authorization": "Bearer " + self._config["MODEL_API_KEY"]},
                            json={"model": self._model, "enable_thinking": False,
                                  "max_tokens": self.max_output_tokens,
                                  "messages": [{"role": "user", "content": content}]}, follow_redirects=False)
                    except httpx.TimeoutException:
                        raise VisionError("MODEL_TIMEOUT", "视觉请求超时，费用未知，预算保持占用。") from None
                    except httpx.RequestError:
                        raise VisionError("MODEL_REQUEST_FAILED", "视觉请求失败，预算保持占用待核对。") from None
                    if response.status_code != 200:
                        raise VisionError("MODEL_HTTP_ERROR", "视觉服务返回错误，预算保持占用待核对。")
                    try:
                        if len(response.content) > 200_000:
                            raise ValueError("response size")
                        envelope = response.json()
                        requests[-1] = self._usage(envelope.get("usage"), envelope.get("id"), 0, False)
                        choice = envelope["choices"][0]
                        if choice.get("finish_reason") != "stop" or response.headers.get("x-dashscope-partialresponse") == "true":
                            raise ValueError("incomplete response")
                        result = parse_observations(choice["message"]["content"], frames)
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        raise VisionError("INVALID_MODEL_OUTPUT", "视觉返回未通过完整性或结构校验。") from None
                    observations.extend(result.observations)
                    uncertainties.extend(result.uncertainties)
                    observed = {item.frame_id for item in result.observations}
                    if len(observed) != len(frames):
                        uncertainties.append("本组部分采样帧没有可用观察，未据此补写内容。")
            uncertainties.extend(["结果全部待人工核对，不是已确认事实、人物身份或原话。",
                                  "仅检查稀疏静态帧；采样间隔未连续观察，不能推断完整动作或事件边界。",
                                  "未分析音轨；时间为抽帧定位值，需回看原片确认。"])
            return {"result": VisionResult(observations=observations,
                        uncertainties=list(dict.fromkeys(uncertainties)), coverage=list(prepared.coverage)),
                    "usage": _aggregate(requests)}
        except DirectorError as exc:
            exc.usage = _aggregate(requests)
            raise
        except Exception:
            raise VisionError("CLIENT_FAILURE", "视觉客户端执行失败，请核对预算状态。", usage=_aggregate(requests)) from None
