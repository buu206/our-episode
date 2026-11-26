import subprocess
import pytest
from app.services import media, playback_review, render_review


def make(tmp_path, video='testsrc2=s=96x64:r=10:d=4', audio='sine=frequency=440:duration=4', offset=0):
    path = tmp_path/'playback.mp4'
    args = [media._binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', video]
    if audio:
        args += ['-itsoffset', str(offset), '-f', 'lavfi', '-i', audio]
    subprocess.run(args + ['-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)],
                   check=True, capture_output=True, timeout=30)
    return path


def test_motion_and_complete_audio_have_no_signal_warnings(tmp_path):
    report = render_review.inspect_output(make(tmp_path), expect_audio=True)
    assert not report['issues']
    assert report['frame_scan']['frames_checked'] == 20
    assert report['audio_span']['checked']
    assert '故事与综艺观感' in report['not_checked']


@pytest.mark.parametrize('video,code', [('color=black:s=96x64:r=10:d=4', 'DARK_INTERVAL'),
                                       ('color=red:s=64x96:r=10:d=4', 'STILL_INTERVAL')])
def test_real_black_and_static_portrait_have_local_replay_ranges(tmp_path, video, code):
    report = render_review.inspect_output(make(tmp_path, video=video), expect_audio=True)
    assert report['status'] == 'needs_attention'
    assert report['issues'][0]['code'] == code
    assert 0 <= report['issues'][0]['start'] < report['issues'][0]['end'] <= report['duration']
    assert report['frame_scan']['scope'] == 'sampled_frame_signal'


def test_short_black_flash_does_not_become_a_long_gap(tmp_path):
    path = make(tmp_path, video='color=black:s=96x64:r=10:d=0.2', audio=None)
    assert render_review.inspect_output(path, expect_audio=False)['issues'] == []


def test_partial_audio_flags_both_ends_and_preserves_output_time(tmp_path):
    path = make(tmp_path, audio='sine=frequency=440:duration=2', offset=1)
    report = render_review.inspect_output(path, expect_audio=True)
    rows = {row['code']:row for row in report['issues']}
    assert rows['AUDIO_START_GAP']['start'] == 0
    assert .8 < rows['AUDIO_START_GAP']['end'] < 1.1
    assert 2.8 < rows['AUDIO_END_GAP']['start'] < 3.1
    assert rows['AUDIO_END_GAP']['end'] == 4
    assert render_review.inspect_output(path, expect_audio=False)['issues'] == []


def test_delayed_silent_audio_replay_is_relative_to_video(tmp_path):
    path = make(tmp_path, audio='anullsrc=r=8000:cl=mono:d=2', offset=1)
    report = render_review.inspect_output(path, expect_audio=True)
    quiet = next(row for row in report['issues'] if row['code'] == 'QUIET_INTERVAL')
    assert .8 < quiet['start'] < 1.1 and 2.8 < quiet['end'] < 3.1


def test_invalid_audio_timing_is_not_a_clean_span(monkeypatch, tmp_path):
    monkeypatch.setattr(media, '_run', lambda *a, **kw: b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}')
    assert playback_review.inspect_audio_span(tmp_path/'unused',4) == {
        'scope':'stream_timing','checked':False,'issues':[]}


def test_slow_changes_do_not_accumulate_as_frozen_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(media, '_run', lambda *a, **kw: bytes(range(100,120)))
    report = playback_review.inspect_frames(tmp_path/'unused', {'width':1,'height':1,'duration':4})
    assert not report['issues']


def test_incomplete_decoded_frame_fails_instead_of_a_clean_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(media, '_run', lambda *a, **kw: b'bad')
    with pytest.raises(media.MediaError):
        playback_review.inspect_frames(tmp_path/'unused', {'width':2,'height':2,'duration':4})
