"""Explicitly positioned visual accents. No detection, identity or emotion inference.

The original shot remains under a live magnifier. A comic-space cutaway is
visibly labelled as a graphic interlude. Source audio is stream-copied unchanged.
All positions/times are bounded data, never model-supplied filter expressions.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from . import media
from .editing import VisualBeat, VisualTarget, timeline
from .variety import _font, _star


def plan(segments: list[dict], offset: float = 0) -> list[tuple[VisualBeat, float]]:
    if not math.isfinite(offset) or offset < 0:
        raise ValueError('invalid intro offset')
    times = timeline(segments)
    beats = []
    for index, (segment, timing) in enumerate(zip(segments, times)):
        outgoing = times[index + 1].overlap if index + 1 < len(times) else 0
        for raw in segment.get('visual_beats', []):
            beat = VisualBeat.model_validate(raw)
            if segment.get('_preserve_expression'):
                raise ValueError('protected expression')
            at = beat.source_time - segment['start']
            if (at < timing.overlap - 1e-6 or at + beat.duration >
                    min(segment['end'] - segment['start'], timing.duration - outgoing) + 1e-6):
                raise ValueError('visual beat overlaps cut or transition')
            beats.append((beat, timing.start + offset + at))
    if len(beats) > 12:
        raise ValueError('too many visual beats')
    ordered = sorted(beats, key=lambda item: item[1])
    if any(a + x.duration > b + 1e-6 for (x, a), (_, b) in zip(ordered, ordered[1:])):
        raise ValueError('overlapping visual beats')
    return ordered


def _word(canvas: Image.Image, text: str, center: tuple[float, float], size: int, angle=0,
          color='#ffdc4c') -> None:
    if not text:
        return
    font = _font(size, text)
    box = font.getbbox(text)
    patch = Image.new('RGBA', (math.ceil(font.getlength(text)) + 30, box[3] - box[1] + 32))
    draw = ImageDraw.Draw(patch)
    pos = (14, 12 - box[1])
    draw.text((pos[0] + 4, pos[1] + 5), text, font=font, fill='#291c39', stroke_width=6, stroke_fill='#291c39')
    draw.text(pos, text, font=font, fill=color, stroke_width=5, stroke_fill='#fff8ed')
    draw.text(pos, text, font=font, fill=color, stroke_width=2, stroke_fill='#291c39')
    patch = patch.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    x = max(4, min(canvas.width - patch.width - 4, round(center[0] - patch.width / 2)))
    y = max(4, min(canvas.height - patch.height - 4, round(center[1] - patch.height / 2)))
    canvas.alpha_composite(patch, (x, y))


def graphics(beat: VisualBeat, width: int, height: int):
    """Return foreground/background, circle diameter and its top-left."""
    layer = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(layer)
    scale = min(width, height) / 720
    if beat.kind == 'burst':
        cx = (beat.target.x + beat.target.width / 2) * width
        cy = (beat.target.y + beat.target.height / 2) * height
        rx = max(100 * scale, beat.target.width * width * .75)
        ry = max(85 * scale, beat.target.height * height * .65)
        for index, angle in enumerate((-160, -100, -35, 30)):
            a = math.radians(angle)
            _word(layer, beat.text, (cx + rx * math.cos(a), cy + ry * math.sin(a)),
                  round((58 + index * 6) * scale), (-12, 9, -8, 13)[index],
                  ('#ffdc4c', '#ff829d', '#fff8ed', '#95efee')[index])
        return layer, 0, 0, 0
    diameter = round(min(width * (.74 if beat.kind == 'comic_space' else .53), height * .52))
    margin = round(26 * scale)
    if beat.kind == 'comic_space':
        draw.rectangle((0, 0, width, height), fill='#19142f')
        for index in range(44):
            sx, sy = (index * 173 + 29) % width, (index * 277 + 31) % height
            _star(draw, sx, sy, (2 + index % 4) * scale, '#fff8ed')
        # Deliberately drawn planet, not a fabricated photographic location.
        planet_y = int(height * .81)
        draw.ellipse((width * .08, planet_y, width * .92, height * 1.17), fill='#6676dc')
        draw.arc((width * -.08, planet_y - 20 * scale, width * 1.08, height * 1.04), 0, 350,
                 fill='#f8cf70', width=max(1, round(10 * scale)))
        _word(layer, '综艺小剧场', (width / 2, height * .085), round(30 * scale), color='#b9b3ef')
        _word(layer, beat.text or '暂停一下', (width / 2, height * .19), round(65 * scale), -5)
        x, y = round((width - diameter) / 2), round(height * .32)
    else:
        x = margin if beat.position.endswith('left') else width - diameter - margin
        y = round(height * (.10 if beat.position.startswith('top') else .52))
        y = min(y, height - diameter - margin)
        cx = (beat.target.x + beat.target.width / 2) * width
        cy = (beat.target.y + beat.target.height / 2) * height
        draw.line((cx, cy, x + diameter / 2, y + diameter / 2), fill='#ffdc4c', width=max(1, round(4 * scale)))
        draw.ellipse((cx - 12 * scale, cy - 12 * scale, cx + 12 * scale, cy + 12 * scale),
                     outline='#ffdc4c', width=max(1, round(3 * scale)))
        _word(layer, beat.text, (x + diameter / 2, y + diameter + 37 * scale), round(43 * scale), -5)
    # The hole is transparent so the live source crop can be composited beneath.
    draw.ellipse((x - 8 * scale, y - 8 * scale, x + diameter + 8 * scale, y + diameter + 8 * scale),
                 fill='#ffdc4c')
    draw.ellipse((x - 3 * scale, y - 3 * scale, x + diameter + 3 * scale, y + diameter + 3 * scale), fill='#fff8ed')
    draw.ellipse((x, y, x + diameter, y + diameter), fill=(0, 0, 0, 0))
    return layer, diameter, x, y


def decorate_visual_beats(base: Path, output: Path, segments: list[dict], temp: Path, *, offset=0) -> dict:
    try:
        beats = plan(segments, offset)
    except (ValueError, TypeError, KeyError) as exc:
        raise media.MediaError('INVALID_VISUAL_BEAT', '视觉强调的位置、时长或片段边界无效。') from exc
    if output.exists() or output.is_symlink():
        raise media.MediaError('MEDIA_OUTPUT_EXISTS', '目标已存在，请另存新版本。')
    source = media._inspect(base)
    width, height = source.width, source.height
    if not beats or beats[-1][1] + beats[-1][0].duration > source.duration + .04:
        raise media.MediaError('INVALID_VISUAL_BEAT', '视觉强调超出成片。')
    args = ['-nostdin', '-v', 'error', '-xerror', '-n', '-threads', '2', '-filter_complex_threads', '1',
            '-protocol_whitelist', 'file,pipe', '-i', str(base)]
    patches = [(i, b) for i, (b, _) in enumerate(beats) if b.kind in ('focus', 'comic_space')]
    filters = ([f'[0:v]split={len(patches)+1}[v0]' + ''.join(f'[src{i}]' for i, _ in patches)]
               if patches else ['[0:v]null[v0]'])
    next_input = 1
    times = timeline(segments)
    for i, (beat, start) in enumerate(beats):
        end = start + beat.duration
        segment = next(s for s, t in zip(segments, times) if t.start - .000001 <= start-offset < t.end)
        if beat.kind in ('cutout', 'headline'):
            from .sticker_beats import render_frames
            from .automatic_treatment import subtitle_layout
            from .dialogue_captions import caption_timeline
            nearby=[line['text'] for line in caption_timeline(segments,offset)
                    if line['start']<end+.5 and line['end']>start-.5]
            # Joined speakers and wrapped lines use the same font/layout as subtitles.
            safe_bottom=None
            if nearby:
                _,_,size,y=subtitle_layout(width,height,' / '.join(dict.fromkeys(nearby)))
                safe_bottom=max(8,int(y-size*.62-12))
            sequence = render_frames(beat, Path(segment['path']), temp / f'sticker-{i}', width, height,
                                     safe_bottom=safe_bottom)
            args += ['-framerate', '30', '-t', f'{beat.duration:.6f}', '-i', str(sequence)]
            filters.append(f'[{next_input}:v]format=rgba,setpts=PTS+{start:.6f}/TB[g{i}]')
            next_input += 1
            filters.append(f'[v{i}][g{i}]overlay=x=0:y=0:eof_action=pass:repeatlast=0[v{i+1}]')
            continue
        original = media._inspect(Path(segment['path']))
        fit = min(width/original.width, height/original.height)
        fw, fh = original.width*fit, original.height*fit
        ox, oy = (width-fw)/2, (height-fh)/2
        r = beat.target
        # Letterboxing can make a valid source target <8% of the output. This
        # internal projection is already bounded by the validated source box.
        display_target = VisualTarget.model_construct(x=(ox+r.x*fw)/width, y=(oy+r.y*fh)/height,
                                                      width=r.width*fw/width, height=r.height*fh/height)
        layer, diameter, x, y = graphics(beat.model_copy(update={'target':display_target}), width, height)
        path = temp / f'beat-{i}-graphic.png'; layer.save(path)
        args += ['-loop', '1', '-framerate', '30', '-t', f'{beat.duration:.6f}', '-i', str(path)]
        graphic_input = next_input; next_input += 1
        fade = (f'fade=t=in:st=0:d=0.08:alpha=1,fade=t=out:st={beat.duration-.12:.6f}:d=0.12:alpha=1,')
        filters.append(f'[{graphic_input}:v]format=rgba,{fade}setpts=PTS+{start:.6f}/TB[g{i}]')
        current = f'v{i}'
        if diameter:
            mask = Image.new('L', (diameter, diameter)); ImageDraw.Draw(mask).ellipse((0, 0, diameter-1, diameter-1), fill=255)
            mask_path = temp / f'beat-{i}-mask.png'; mask.save(mask_path)
            args += ['-loop', '1', '-framerate', '30', '-t', f'{beat.duration:.6f}', '-i', str(mask_path)]
            mask_input = next_input; next_input += 1
            cw, ch = max(2, int(r.width * fw)//2*2), max(2, int(r.height * fh)//2*2)
            cx, cy = min(int(ox+r.x*fw)//2*2, width-cw), min(int(oy+r.y*fh)//2*2, height-ch)
            filters.append(f'[src{i}]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,'
                           f'crop={cw}:{ch}:{cx}:{cy},scale={diameter}:{diameter}:force_original_aspect_ratio=increase,'
                           f'crop={diameter}:{diameter},format=rgba[p{i}]')
            filters.append(f'[{mask_input}:v]format=gray[m{i}]')
            filters.append(f'[p{i}][m{i}]alphamerge,{fade}setpts=PTS+{start:.6f}/TB[f{i}]')
            # Space graphic is opaque except its round window: install it first.
            if beat.kind == 'comic_space':
                filters.append(f'[{current}][g{i}]overlay=eof_action=pass:repeatlast=0[s{i}]')
                filters.append(f'[s{i}][f{i}]overlay=x={x}:y={y}:eof_action=pass:repeatlast=0[v{i+1}]')
                continue
            filters.append(f'[{current}][f{i}]overlay=x={x}:y={y}:eof_action=pass:repeatlast=0[s{i}]')
            current = f's{i}'
        filters.append(f'[{current}][g{i}]overlay=x=0:y=0:eof_action=pass:repeatlast=0[v{i+1}]')
    args += ['-filter_complex', ';'.join(filters), '-map', f'[v{len(beats)}]', '-map', '0:a:0',
             '-t', f'{source.duration:.6f}', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
             '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-map_metadata', '-1',
             '-movflags', '+faststart', str(output)]
    media._run('ffmpeg', args, timeout=90+source.duration*12, code='MEDIA_RENDER_FAILED')
    info = media.probe(output)
    if abs(info['duration']-source.duration) > .12 or not info['has_audio'] or (info['width'], info['height']) != (width,height):
        raise media.MediaError('MEDIA_RENDER_FAILED', '视觉强调后的画幅、声音或时长校验失败。')
    return {'visual_beats_revision': 'sticker-v3', 'visual_beats_count': len(beats),
            'reviewed_freeze_cutouts': sum(b.kind == 'cutout' for b, _ in beats)}
