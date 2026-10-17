"""Create the source download without local environments or generated files."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
output = root / 'dist' / 'memory-aware-runner-0.1.0-source.zip'
output.parent.mkdir(exist_ok=True)
files = [root / name for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'MANIFEST.in')]
for name in ('src', 'tests', 'examples', 'tools'):
    files.extend(p for p in (root / name).rglob('*') if p.is_file()
                 and '__pycache__' not in p.parts and not p.name.endswith(('.pyc', '.pyo'))
                 and not any(part.endswith('.egg-info') for part in p.parts))
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for file in sorted(files):
        archive.write(file, Path('memory-aware-runner') / file.relative_to(root))
print(output)
