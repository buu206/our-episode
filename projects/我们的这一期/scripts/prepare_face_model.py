#!/usr/bin/env python3
"""Explicit download of a pinned official model; never uploads media or reads keys."""
import hashlib
from pathlib import Path
import sys
import tempfile
import urllib.request

PRODUCT = Path(__file__).resolve().parents[1]
URL = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
SHA256 = '64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff'
SIZE = 3758596


def main():
    target = PRODUCT / 'data/models/face_landmarker.task'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        data = target.read_bytes() if target.stat().st_size == SIZE else b''
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('Existing model differs; not overwritten')
    else:
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read(SIZE + 1)
        if len(data) != SIZE or hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('Model verification failed')
        # Publish only a fully downloaded and verified file.
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    print('Face model ready; verified SHA-256. No media uploaded.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError):
        print('Face model preparation failed; check network or existing file.', file=sys.stderr)
        sys.exit(1)
