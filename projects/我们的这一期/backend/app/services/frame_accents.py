"""Bounded frame-edge accents, with original audio copied unchanged."""
from pathlib import Path
import math
from . import media
from .editing import EditSegment, timeline

COLORS = {'coral': '0xff527b', 'amber': '0xffcb45', 'mint': '0x20cbb5'}


def plan(segments, offset=0.):
    if not math.isfinite(offset) or offset < 0:
        raise ValueError('Invalid narration offset')
    result = []
    timings = timeline(segments)
    for i, (raw, timing) in enumerate(zip(segments, timings)):
        if not raw.get('frame_accents'):
            continue
        # Runtime rows also carry controlled paths and derived subtitle fields.
        shot = EditSegment.model_validate({k: v for k, v in raw.items() if k in EditSegment.model_fields})
        if shot.frame_accents and raw.get('_preserve_expression'):
            raise ValueError('采访表达不能添加边框。')
        outgoing = timings[i + 1].overlap if i + 1 < len(timings) else 0
        for accent in shot.frame_accents:
            relative = accent.source_time - shot.start
            if relative < timing.overlap or relative + accent.duration > timing.duration - outgoing + 1e-6:
                raise ValueError('边框不能覆盖转场。')
            result.append({'start': offset + timing.start + relative,
                           'end': offset + timing.start + relative + accent.duration,
                           'palette': accent.palette})
    if len(result) > 6:
        raise ValueError('一期最多六处边框。')
    return result


def render(base: Path, output: Path, segments, offset=0.):
    accents = plan(segments, offset)
    if not accents:
        return base, {'frame_accents': []}
    if output.exists() or output.is_symlink():
        raise media.MediaError('MEDIA_OUTPUT_EXISTS', '边框输出已存在，请保存新版本。')
    source = media.probe(base)
    width = max(3, round(min(source['width'], source['height']) * .012))
    filters = [f"drawbox=x=0:y=0:w=iw:h=ih:color={COLORS[a['palette']]}:t={width}:"
               f"enable='gte(t,{a['start']:.6f})*lt(t,{a['end']:.6f})'" for a in accents]
    media._run('ffmpeg', ['-nostdin', '-v', 'error', '-xerror', '-threads', '2', '-filter_threads', '1',
        '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov', '-enable_drefs', '0',
        '-use_absolute_path', '0', '-i', str(base), '-vf', ','.join(filters), '-map', '0:v:0',
        '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2',
        '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-map_metadata', '-1', '-movflags', '+faststart', str(output)],
        timeout=90 + source['duration'] * 8, code='MEDIA_RENDER_FAILED')
    actual = media.probe(output)
    if any(actual[k] != source[k] for k in ('width', 'height', 'has_audio')) or abs(actual['duration'] - source['duration']) > .12:
        raise media.MediaError('MEDIA_RENDER_FAILED', '边框输出校验失败。')
    return output, {'frame_accents': accents}
