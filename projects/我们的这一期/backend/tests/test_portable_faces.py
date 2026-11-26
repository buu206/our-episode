from types import SimpleNamespace
import json
import subprocess

import pytest

from app.services import automatic_treatment as treatment, portable_faces as portable
from app.services.native.face_landmarks_mediapipe import convert_landmarks, LEFT_EYE, RIGHT_EYE, MOUTH
from test_automatic_treatment import face


def test_portable_threshold_is_not_an_invented_confidence_score():
    points = [SimpleNamespace(x=.3 + (i % 3) * .1, y=.3 + (i % 4) * .1) for i in range(478)]
    # An upright synthetic face, rather than a random grid with the mouth above
    # its eyes. Keep the provider confidence and malformed-output checks below.
    for i in LEFT_EYE + RIGHT_EYE:points[i].y=.4
    for i in MOUTH:points[i].y=.55
    result = convert_landmarks(points)
    assert 'confidence' not in result
    assert treatment.valid_face(result)
    points[33].x = float('nan')
    assert convert_landmarks(points) is None
    points[33].x = 1.1
    assert convert_landmarks(points) is None
    assert convert_landmarks(points[:20]) is None


def test_platform_dispatch_preserves_native_and_uses_portable_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(treatment.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(portable, 'detect', lambda base, folder: ([], 'portable_test'))
    assert treatment.detect_faces(tmp_path/'v.mp4', tmp_path) == ([], 'portable_test')


def test_missing_or_tampered_model_does_not_run_or_download(monkeypatch, tmp_path):
    monkeypatch.setattr(portable, 'PRODUCT', tmp_path)
    monkeypatch.setattr(portable.subprocess, 'run', lambda *a, **k: pytest.fail('must not execute'))
    assert portable.detect(tmp_path/'v.mp4', tmp_path)[1] == 'portable_model_missing'
    model = tmp_path/'data/models/face_landmarker.task'; model.parent.mkdir(parents=True);model.write_bytes(b'bad')
    assert portable.detect(tmp_path/'v.mp4', tmp_path)[1] == 'portable_model_invalid'


@pytest.mark.parametrize('fault', ['timeout', 'failed', 'invalid_json', 'invalid_time', 'invalid_face', 'valid'])
def test_worker_failure_and_geometry_validation(monkeypatch, tmp_path, fault):
    monkeypatch.setattr(portable, 'PRODUCT', tmp_path)
    model = tmp_path/'data/models/face_landmarker.task';model.parent.mkdir(parents=True);model.write_bytes(b'x'*3758596)
    import hashlib
    monkeypatch.setattr(portable, 'MODEL_SHA256', hashlib.sha256(model.read_bytes()).hexdigest())
    monkeypatch.setattr(portable.media, '_run', lambda *a, **k: None)
    def run(*args, **kwargs):
        assert kwargs['timeout'] == 180
        if fault == 'timeout':raise subprocess.TimeoutExpired('local-detector', 180)
        row = {'time': 1 if fault == 'invalid_time' else 0, 'faces': [dict(face(), confidence=.1) if fault == 'invalid_face' else face()]}
        rows = [dict(row, time=row['time'] + i/5) for i in range(3)]
        return SimpleNamespace(returncode=1 if fault == 'failed' else 0,
                               stdout=b'invalid' if fault == 'invalid_json' else json.dumps(rows).encode())
    monkeypatch.setattr(portable.subprocess, 'run', run)
    rows, status = portable.detect(tmp_path/'v.mp4', tmp_path)
    if fault in ('valid', 'invalid_face'):
        assert status == 'detected_mediapipe'
        assert bool(rows[1]['faces']) == (fault == 'valid')
        assert rows[0]['faces'] == rows[2]['faces'] == []
    else:
        assert rows == [] and status in ('portable_detector_failed', 'portable_output_invalid')


def test_landmarks_with_infinite_confidence_or_missing_eye_are_rejected():
    assert not treatment.valid_face(dict(face(), confidence=float('inf')))
    assert not treatment.valid_face(dict(face(), eyes=[[], []]))


def test_temporal_filter_drops_isolated_detection_and_face_switch():
    rows = [{'time': i/5, 'faces': [face()]} for i in range(5)]
    rows[1]['faces'] = []
    assert [bool(r['faces']) for r in portable.stable_faces(rows)] == [False, False, False, True, False]
    rows[1]['faces'] = [dict(face(), box=[.7, .7, .2, .2])]
    assert portable.stable_faces(rows)[1]['faces'] == []
