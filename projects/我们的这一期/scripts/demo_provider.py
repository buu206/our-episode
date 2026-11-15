"""Explicit, offline workflow fixtures. Never imported by the normal server.

No model, network, credentials, transcript, or scene interpretation is used.
Only the demo entry point opts in to these fixed results.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from app.services import media
from app.services.director import DirectorError, parse_result


DEMO_REASON = "流程演示：固定测试结果，不调用AI、不外发、不扣费"


@dataclass(frozen=True)
class DemoPreparedVideo:
    sources: tuple[tuple[str, float], ...]
    required_reservation_yuan: float = 0.01

    @property
    def request_digest(self):
        return hashlib.sha256(json.dumps(self.sources, separators=(",", ":")).encode()).hexdigest()


class DemoServices:
    """ModelServices facade implemented with explicitly labelled test fixtures."""

    is_real = False

    def profile(self):
        return {"mode": "demo-fixture"}

    def director_estimate(self):
        return 0.01

    def vision_estimate(self, prepared):
        return prepared.required_reservation_yuan

    def ready(self):
        return True, DEMO_REASON

    def chat_ready(self, context=None):
        return True, DEMO_REASON

    def voice_ready(self):
        return True, DEMO_REASON

    async def director_voice(self, chunks, reservation, permit, context):
        await self._permit(permit, reservation, amount=0)
        return self._result([{'asset_id': chunk.asset_id, 'start': chunk.offset,
                             'end': chunk.offset + chunk.duration,
                             'text': '演示占位，未识别录音。'} for chunk in chunks])

    def chat_estimate(self, context=None):
        return 0.01

    def directions_ready(self, context=None):
        return self.ready()

    def scoped_director_estimate(self, context=None):
        return self.director_estimate()

    async def scoped_directions(self, evidence, assets, background, reservation, permit, context):
        return await self.directions(evidence, assets, background, reservation, permit)

    async def director_chat(self, context, reservation, permit):
        await self._permit(permit, reservation)
        question = context.get('question', '')
        available = context.get('available_actions', [])
        action = None
        if '旁白' in question and 'revise_narration' in available:
            action = {'kind': 'revise_narration', 'title': '演示：另存短旁白草案',
                      'explanation': '固定测试建议。确认后只另存草案，不生成语音或覆盖原片。',
                      'narration_text': '在场，开拍！'}
        elif '方案' in question and 'revise_directions' in available:
            action = {'kind': 'revise_directions', 'title': '演示：生成新方向候选',
                      'explanation': '固定测试建议。确认后按现有已核对区间生成演示候选。',
                      'requirement': '保留完整原声和连续动作，减少零碎镜头。'}
        return self._result({'reply': '这是固定流程演示，不是真实AI回答。这里用于核对多轮消息保存、修改建议确认与另存草案。',
                             'action': action})

    @staticmethod
    def _result(result):
        return {"result": result, "usage": {
            "model": "DEMO-FIXTURE", "cost_yuan": 0, "cost_status": "known",
            "real_call": False, "input_tokens": 0, "output_tokens": 0,
            "retries": 0,
        }}

    @staticmethod
    async def _permit(permit, reservation, digest=None, amount=0.01):
        allowed = permit(reservation, amount, digest) if digest is not None else permit(reservation, amount)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            raise DirectorError("DEMO_PERMIT_REJECTED", "演示步骤未获本地任务许可，未生成结果。",
                                usage=DemoServices._result(None)["usage"])

    def prepare_vision(self, assets, media_root):
        root = Path(media_root).resolve(strict=True)
        sources = []
        for item in assets:
            path = Path(item["path"])
            if path.is_symlink() or path.resolve(strict=True).parent != root:
                raise DirectorError("INVALID_LOCAL_ASSET", "演示只读取本期上传的本地素材副本。")
            duration = float(media.probe(path)["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise DirectorError("INVALID_LOCAL_ASSET", "本地素材时长无效。")
            sources.append((item["asset_id"], duration))
        if not sources:
            raise DirectorError("INVALID_ASSETS", "演示需要至少一份本地素材。")
        return DemoPreparedVideo(tuple(sources))

    async def vision(self, prepared, reservation, permit):
        await self._permit(permit, reservation, prepared.request_digest)
        observations, coverage = [], []
        for asset_id, duration in prepared.sources:
            times = [duration / 3, duration * 2 / 3]
            for index, seconds in enumerate(times):
                observations.append({"asset_id": asset_id, "time": seconds,
                    "frame_id": f"demo-{asset_id}-{index + 1}",
                    "note": "演示占位，未进行画面理解；此条仅验证时间区间和人工确认流程。",
                    "verification": "unverified"})
            coverage.append({"asset_id": asset_id, "duration": duration,
                "sample_times": [], "placeholder_times": times,
                "unsampled_intervals": [{"start": 0, "end": duration}],
                "max_gap_seconds": duration, "method": "demo_time_markers",
                "audio_analyzed": False, "time_basis": "demo_placeholder"})
        return self._result({"observations": observations, "coverage": coverage,
            "uncertainties": ["固定演示数据：没有抽帧、画面理解或原声识别，不能用来判断真实内容。"],
            "verification": "unverified"})

    def prepare_audio(self, asset, path, directory):
        # No audio is read or extracted. The real upload probe already stored duration.
        duration = float(asset.duration)
        return [{"asset_id": asset.id, "start": 0.0, "end": min(duration, 5.0)}]

    def asr_estimate(self, chunks):
        return 0.01

    async def asr(self, chunks, reservation, permit):
        await self._permit(permit, reservation)
        return self._result([{**chunk, "text": "演示占位，非真实转写；未识别原声。"} for chunk in chunks])

    async def directions(self, evidence, assets, background, reservation, permit):
        await self._permit(permit, reservation)
        spans = []
        # Only previously reviewed source intervals are used. Names/background do
        # not become invented plot points; even user notes are not treated as AI findings.
        for row in evidence:
            asset_id, start, end = row["asset_id"], float(row["start"]), float(row["end"])
            if asset_id not in assets or not 0 <= start < end <= assets[asset_id]:
                raise DirectorError("INVALID_EVIDENCE", "演示方向引用区间无效。",
                                    usage=self._result(None)["usage"])
            spans.append({"asset_id": asset_id, "start": start, "end": end,
                          "note": "演示占位片段：仅验证选段与渲染，不描述素材情节。"})
        spans = spans[:40]
        directions = []
        if spans:
            for identifier, title, chosen in (
                ("demo_sequence", "演示：按已确认区间顺序剪接", spans),
                ("demo_short", "演示：只取首个已确认区间", spans[:1]),
            ):
                directions.append({"id": identifier, "title": title,
                    "summary": "固定流程选项，不是 AI 发现的故事。用于验证选方向、编辑、合成和确认。",
                    "evidence": chosen})
        data = {"directions": directions, "uncertainties": [
            "固定演示：未理解画面或对白；后续片头音频是合成提示音，不是真实旁白。"]}
        return self._result(parse_result(json.dumps(data, ensure_ascii=False), assets))

    def tts_estimate(self, text):
        return 0.01

    async def tts(self, text, output, reservation, permit):
        await self._permit(permit, reservation)
        path = Path(output)
        if path.exists() or path.suffix.lower() != ".wav" or not path.parent.is_dir():
            raise DirectorError("INVALID_AUDIO_OUTPUT", "演示提示音输出位置无效。",
                                usage=self._result(None)["usage"])
        rate = 24000
        # A gentle one-second sine tone with fades, never a synthetic human voice.
        samples = [round(2400 * min(i / 1200, (rate - 1 - i) / 1200, 1)
                         * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
        with path.open("xb") as file:
            with wave.open(file, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(struct.pack("<" + "h" * len(samples), *samples))
        return self._result({"path": str(path), "duration": 1.0,
                             "demo_audio": "合成提示音，非真实旁白"})
