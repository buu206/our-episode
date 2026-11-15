"""Optional portable face geometry; installation is explicit, never done by a job."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import media

PRODUCT = Path(__file__).resolve().parents[3]
MODEL_SHA256 = '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff'


def stable_faces(rows):
    """Require matching geometry on both neighbouring samples (0.4 seconds).

    An isolated false detection on clothing must not become an effect anchor.
    Keep empty samples so interpolation cannot bridge rejected intervals.
    """
    from .automatic_treatment import overlap
    result = []
    for index, row in enumerate(rows):
        faces = []
        if 0 < index < len(rows) - 1:
            before, after = rows[index-1], rows[index+1]
            if (abs(row['time'] - before['time'] - .2) < .001 and
                    abs(after['time'] - row['time'] - .2) < .001):
                faces = [face for face in row['faces'] if all(
                    any(overlap(face['box'], other['box']) >= .6 for other in neighbour['faces'])
                    for neighbour in (before, after))]
        result.append({**row, 'faces': faces})
    return result


def detect(base, folder):
    model = PRODUCT / 'data/models/face_landmarker.task'
    if not model.is_file():
        return [], 'portable_model_missing'
    try:
        if model.stat().st_size != 3758596 or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
            return [], 'portable_model_invalid'
        # Isolated optional runtime on developer machines; Linux can install
        # requirements-vision.txt into its application environment instead.
        isolated = PRODUCT / 'data/runtime/vision-venv/bin/python'
        python = str(isolated) if isolated.is_file() else sys.executable
        frames = folder / 'portable-observations'
        frames.mkdir()
        media._run('ffmpeg', ['-nostdin', '-v', 'error', '-i', str(base), '-t', '90',
            '-vf', 'fps=5:start_time=0,scale=640:640:force_original_aspect_ratio=decrease',
            '-frames:v', '450', '-q:v', '3', str(frames / '%04d.jpg')],
            timeout=180, code='MEDIA_RENDER_FAILED')
        script = Path(__file__).parent / 'native/face_landmarks_mediapipe.py'
        # Suppress native diagnostics in user logs. They never become API errors.
        env = {**os.environ, 'GLOG_minloglevel': '2', 'MPLCONFIGDIR': str(folder / 'mpl-cache')}
        run = subprocess.run([python, str(script), str(frames), str(model)],
                             capture_output=True, timeout=180, env=env)
        if run.returncode:
            return [], 'portable_detector_failed'
        rows = json.loads(run.stdout)
        from .automatic_treatment import valid_face
        if not isinstance(rows, list) or len(rows) > 450:
            return [], 'portable_output_invalid'
        for index, row in enumerate(rows):
            if row['time'] != index / 5 or not isinstance(row['faces'], list) or len(row['faces']) > 4:
                return [], 'portable_output_invalid'
            row['faces'] = [face for face in row['faces'] if valid_face(face)]
        return stable_faces(rows), 'detected_mediapipe'
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return [], 'portable_detector_failed'
