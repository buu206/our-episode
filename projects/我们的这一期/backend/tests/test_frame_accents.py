import io
import subprocess
import pytest
from PIL import Image
from app.schemas import VersionIn
from app.services import media
from app.services.frame_accents import plan, render


def shot(**kw):
    return dict(asset_id='a', start=10., end=12., caption='',
                frame_accents=[dict(source_time=10.5, duration=.5, palette='coral')], **kw)


def test_frame_timing_and_contract():
    assert plan([shot()], 1.5) == [dict(start=2., end=2.5, palette='coral')]
    with pytest.raises(ValueError):
        VersionIn(title='x', segments=[shot()], packaging='original')
    bad = shot(); bad['frame_accents'][0]['source_time'] = 11.9
    with pytest.raises(ValueError):
        VersionIn(title='x', segments=[bad], packaging='variety_v2')
    with pytest.raises(ValueError):
        plan([shot(_preserve_expression=True)])
    assert plan([dict(asset_id='a', start=0., end=.01, caption='')]) == []


@pytest.mark.parametrize('size', ['320x180', '180x320'])
def test_timed_frame_preserves_center_audio_and_duration(tmp_path, size):
    source, output = tmp_path/'source.mp4', tmp_path/'out.mp4'
    ffmpeg = str(media._binary('ffmpeg'))
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', f'color=blue:s={size}:r=30',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                    '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
    render(source, output, [shot()])
    def frame(t):
        raw = subprocess.check_output([ffmpeg, '-v', 'error', '-ss', str(t), '-i', str(output),
            '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
        return Image.open(io.BytesIO(raw)).convert('RGB')
    assert frame(.2).getpixel((1, 1))[2] > 200
    assert frame(.7).getpixel((1, 1))[0] > 200
    assert frame(1.2).getpixel((1, 1))[2] > 200
    image = frame(.7)
    assert image.getpixel((image.width//2, image.height//2))[2] > 200
    def audio(path):
        return subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a', '-c:a', 'copy', '-f', 'adts', '-'])
    assert audio(source) == audio(output)
    assert media.probe(source)['duration'] == pytest.approx(media.probe(output)['duration'], abs=.04)
