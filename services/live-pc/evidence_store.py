import hashlib, json, shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass
class EvidenceStore:
    root: Path
    quota_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        self.root = Path(self.root)
        (self.root / 'clips').mkdir(parents=True, exist_ok=True)
        (self.root / 'keyframes').mkdir(parents=True, exist_ok=True)
        (self.root / 'day_buffer').mkdir(parents=True, exist_ok=True)

    def _sha(self, path: Path) -> str:
        h=hashlib.sha256()
        with path.open('rb') as f:
            for b in iter(lambda:f.read(1024*1024), b''):
                h.update(b)
        return h.hexdigest()

    def store_bytes(self, data: bytes, *, kind: str, name: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        sub = 'keyframes' if kind in {'keyframe','frame'} else 'clips'
        digest=hashlib.sha256(data).hexdigest()
        suffix = Path(name or '').suffix or ('.jpg' if sub=='keyframes' else '.bin')
        path = self.root / sub / f'{digest}{suffix}'
        path.write_bytes(data)
        if metadata is not None:
            path.with_suffix(path.suffix + '.json').write_text(json.dumps(metadata, sort_keys=True), encoding='utf-8')
        return {'kind': kind, 'uri': str(path), 'path': str(path), 'sha256': digest, 'bytes': len(data)}

    def copy_asset(self, source: str | Path, *, kind: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        src=Path(source)
        data=src.read_bytes()
        asset=self.store_bytes(data, kind=kind, name=src.name, metadata=metadata)
        return asset

    def day_buffer_usage(self) -> dict[str, Any]:
        total=0; files=0
        for p in (self.root/'day_buffer').glob('**/*'):
            if p.is_file():
                total += p.stat().st_size; files += 1
        return {'bytes': total, 'files': files, 'quota_bytes': self.quota_bytes, 'ok': total <= self.quota_bytes}

    def purge_day_buffer(self) -> dict[str, Any]:
        d=self.root/'day_buffer'
        before=self.day_buffer_usage()
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        return {'status': 'completed', 'purged_bytes': before['bytes'], 'purged_files': before['files']}
