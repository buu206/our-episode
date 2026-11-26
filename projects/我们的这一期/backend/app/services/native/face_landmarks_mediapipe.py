"""CPU-only geometry worker. No network, recognition, emotion labels or uploads."""
import json
import math
from pathlib import Path
import sys

# MediaPipe's face mesh topology. Only measured contours are exported.
LEFT_EYE = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
RIGHT_EYE = (263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466)
MOUTH = (61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185)
OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
        400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109)


def convert_landmarks(landmarks):
    if len(landmarks) < 468:
        return None
    def points(indices):
        return [[float(landmarks[i].x), float(landmarks[i].y)] for i in indices]
    oval = points(OVAL)
    eyes, mouth = [points(LEFT_EYE), points(RIGHT_EYE)], points(MOUTH)
    if not all(math.isfinite(v) and 0 <= v <= 1 for p in oval + eyes[0] + eyes[1] + mouth for v in p):
        return None  # Never clamp a partially offscreen face into plausible geometry.
    x, y = min(p[0] for p in oval), min(p[1] for p in oval)
    w, h = max(p[0] for p in oval) - x, max(p[1] for p in oval) - y
    return {'box': [x, y, w, h], 'eyes': eyes, 'mouth': mouth, 'contour': oval, 'contour_kind': 'oval',
            'provider': 'mediapipe', 'presence_threshold': .8}


def main():
    import mediapipe as mp
    folder, model = map(Path, sys.argv[1:3])
    files = sorted(folder.glob('*.jpg'))
    if len(files) > 450:
        raise ValueError('frame_limit')
    options = mp.tasks.vision.FaceLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model), delegate=mp.tasks.BaseOptions.Delegate.CPU),
        running_mode=mp.tasks.vision.RunningMode.VIDEO, num_faces=4,
        min_face_detection_confidence=.5, min_face_presence_confidence=.8,
        min_tracking_confidence=.5, output_face_blendshapes=False,
        output_facial_transformation_matrixes=False)
    rows = []
    with mp.tasks.vision.FaceLandmarker.create_from_options(options) as detector:
        for index, file in enumerate(files):
            result = detector.detect_for_video(mp.Image.create_from_file(str(file)), index * 200)
            faces = [convert_landmarks(points) for points in result.face_landmarks]
            rows.append({'time': index / 5, 'faces': [face for face in faces if face is not None]})
    print(json.dumps(rows, allow_nan=False))


if __name__ == '__main__':
    main()
