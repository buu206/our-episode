"""Bounded local playback hints. No semantic vision, speech recognition or edits."""
import json
import math

from . import media

SAMPLE_FPS = 5


def _intervals(flags, duration, minimum):
    start = None
    for index, flagged in enumerate([*flags, False]):
        if flagged and start is None:
            start = index / SAMPLE_FPS
        elif not flagged and start is not None:
            end = min(index / SAMPLE_FPS, duration)
            if end - start + 1e-6 >= minimum:
                yield round(start, 3), round(end, 3)
            start = None


def inspect_frames(path, info):
    # Preserve aspect ratio: artificial padding must not count as black pixels.
    factor = min(1, 160 / max(info['width'], info['height']))
    width, height = max(1, round(info['width'] * factor)), max(1, round(info['height'] * factor))
    raw = media._run('ffmpeg', ['-nostdin', '-v', 'error', '-threads', '2',
        '-i', str(path), '-map', '0:v:0', '-an', '-t', '90.15',
        '-vf', f'setpts=PTS-STARTPTS,fps={SAMPLE_FPS},scale={width}:{height},format=gray',
        '-frames:v', '451', '-f', 'rawvideo', '-'], timeout=120, code='MEDIA_DECODE_FAILED')
    size = width * height
    if not raw or len(raw) % size:
        raise media.MediaError('MEDIA_DECODE_FAILED', '画面检查未能读取完整采样，请重新检查。')
    previous, dark, still = None, [], []
    for offset in range(0, len(raw), size):
        frame = raw[offset:offset + size]
        dark.append(sum(value <= 12 for value in frame) / size >= .98)
        # Compare with the start of a stationary run, not just the last frame:
        # slowly moving footage must not accumulate into a spurious freeze.
        unchanged = previous is not None and sum(abs(a-b) for a,b in zip(frame, previous)) / size <= .35
        still.append(unchanged and not dark[-1])
        if not unchanged or dark[-1]:
            previous = frame
    issues = [dict(code='DARK_INTERVAL', start=start, end=end,
        message='这里画面几乎全黑；如果不是刻意留黑，可以检查转场。')
        for start, end in _intervals(dark, info['duration'], .6)]
    issues += [dict(code='STILL_INTERVAL', start=start, end=end,
        message='这里画面较长时间几乎没动，也可能是定格或静态片头，建议回看。')
        for start, end in _intervals(still, info['duration'], 2.6)]
    return {'scope': 'sampled_frame_signal', 'sample_fps': SAMPLE_FPS,
            'frames_checked': len(dark), 'issues': issues,
            'not_checked': ['短于采样间隔的闪帧', '人物动作含义', '转场审美与综艺效果']}


def inspect_audio_span(path, duration):
    raw = media._run('ffprobe', ['-v', 'error', '-show_entries',
        'stream=codec_type,start_time,duration', '-of', 'json', str(path)],
        timeout=25, code='MEDIA_DECODE_FAILED')
    try:
        streams = json.loads(raw)['streams']
        video = next(row for row in streams if row.get('codec_type') == 'video')
        audio = next(row for row in streams if row.get('codec_type') == 'audio')
        start = float(audio['start_time']) - float(video['start_time'])
        length = float(audio['duration'])
        if not all(math.isfinite(n) for n in (start, length)) or length <= 0:
            raise ValueError()
    except (KeyError, ValueError, TypeError, StopIteration):
        return {'scope': 'stream_timing', 'checked': False, 'issues': []}
    end = start + length
    issues = []
    for code, left, right, message in (
        ('AUDIO_START_GAP', 0., min(start, duration), '开头有一段没有音轨覆盖，检查原声是否晚进来了。'),
        ('AUDIO_END_GAP', max(0., end), duration, '声音比画面先结束了，检查结尾原声是否漏掉。'),
    ):
        if right - left >= .35:
            issues.append(dict(code=code, start=round(left, 3), end=round(right, 3), message=message))
    return {'scope': 'stream_timing', 'checked': True, 'start': round(start, 3),
            'end': round(end, 3), 'issues': issues}
