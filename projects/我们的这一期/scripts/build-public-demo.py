"""Build the explicitly labelled, offline public walkthrough from an allowlist.

No runtime database, media, environment file or API credential is read/copied.
"""
from pathlib import Path
import json
import shutil
import subprocess
import hashlib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'public-demo'
OUTPUT = ROOT / 'data/runtime/public-demo-site'
OUTPUT.mkdir(parents=True, exist_ok=True)
(OUTPUT / 'assets').mkdir(exist_ok=True)
for name in ('index.html', 'style.css'):
    shutil.copyfile(SOURCE / name, OUTPUT / name)
assets = {
    'brand/zaichang-logo.png': 'logo.png',
    'home/friends.png': 'friends.png',
    'home/fonts/episode-title.woff2': 'episode-title.woff2',
    'home/fonts/OFL.txt': 'OFL.txt',
}
for source, target in assets.items():
    shutil.copyfile(ROOT / 'frontend/public' / source, OUTPUT / 'assets' / target)
subprocess.run(['node', str(ROOT / 'frontend/node_modules/typescript/bin/tsc'),
    str(SOURCE / 'app.ts'), '--strict', '--skipLibCheck', '--target', 'ES2020',
    '--lib', 'ES2020,DOM', '--module', 'ES2020', '--outDir', str(OUTPUT)], check=True)
(OUTPUT / '.nojekyll').write_text('')
manifest = {str(path.relative_to(OUTPUT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(OUTPUT.rglob('*')) if path.is_file() and path.name != 'manifest.json'}
(OUTPUT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Built {len(manifest)} allowlisted files: {OUTPUT}')
