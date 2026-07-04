from __future__ import annotations

"""Fetch + verify V19 model weights declared in configs/MODEL_MANIFEST.yaml.

Downloads every manifest entry that carries a ``url`` + ``sha256`` into its
``path`` (default under ``models/``, git-ignored), verifying the checksum. Idempotent:
a file already present with the right sha256 is skipped.

Usage:
    python scripts/fetch_models_v19.py            # fetch ONNX weights (detector)
    python scripts/fetch_models_v19.py --argos    # + install Argos en<->fr packs
    python scripts/fetch_models_v19.py --check     # verify only, no download

faster-whisper (`asr`) and the VLM/LLM (`ollama`) are not fetched here: the
first is pulled into the HuggingFace cache by faster-whisper on first use, the
second via `ollama pull`. This keeps the script to reproducible, sha-verified
static assets (handoff §4.1 / ADR §E27).
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "MODEL_MANIFEST.yaml"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    import yaml

    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    return data.get("models", {}) if isinstance(data, dict) else {}


def _fetchable(models: dict) -> list[tuple[str, dict]]:
    out = []
    for name, spec in models.items():
        if isinstance(spec, dict) and spec.get("url") and spec.get("sha256"):
            out.append((name, spec))
    return out


def fetch_all(*, check_only: bool = False) -> int:
    models = _load_manifest()
    errors = 0
    for name, spec in _fetchable(models):
        path = ROOT / spec.get("path", f"models/{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = spec["sha256"]
        if path.exists():
            actual = _sha256(path)
            if actual == expected:
                print(f"[ok]   {name}: {path.name} present, sha256 verified")
                continue
            print(f"[warn] {name}: {path.name} sha256 mismatch (have {actual[:12]}…)")
            if check_only:
                errors += 1
                continue
        elif check_only:
            print(f"[miss] {name}: {path} absent ({spec['license']})")
            errors += 1
            continue

        url = spec["url"]
        print(f"[get]  {name}: {url} -> {path} ({spec['license']})")
        try:
            urllib.request.urlretrieve(url, path)  # noqa: S310 - manifest-pinned URL
        except Exception as exc:  # pragma: no cover - network failure path
            print(f"[fail] {name}: download error: {exc}")
            errors += 1
            continue
        actual = _sha256(path)
        if actual != expected:
            print(f"[fail] {name}: sha256 mismatch after download (got {actual})")
            errors += 1
        else:
            print(f"[ok]   {name}: downloaded + sha256 verified ({spec['license']})")
    return errors


def install_argos(pairs: list[tuple[str, str]] | None = None) -> int:
    """Install Argos Translate language packages (offline NMT, MIT/CTranslate2)."""
    try:
        from argostranslate import package
    except Exception as exc:  # pragma: no cover
        print(f"[fail] argostranslate not installed: {exc}")
        return 1
    pairs = pairs or [("en", "fr"), ("fr", "en"), ("zh", "fr")]
    print("[argos] updating package index…")
    package.update_package_index()
    available = package.get_available_packages()
    errors = 0
    for src, tgt in pairs:
        match = next((p for p in available if p.from_code == src and p.to_code == tgt), None)
        if match is None:
            print(f"[argos] no package {src}->{tgt} in index (skipped)")
            continue
        installed = {(p.from_code, p.to_code) for p in package.get_installed_packages()}
        if (src, tgt) in installed:
            print(f"[argos] {src}->{tgt} already installed")
            continue
        try:
            print(f"[argos] installing {src}->{tgt} …")
            package.install_from_path(match.download())
        except Exception as exc:  # pragma: no cover
            print(f"[argos] {src}->{tgt} install failed: {exc}")
            errors += 1
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch + verify V19 model weights.")
    ap.add_argument("--check", action="store_true", help="verify only, no download")
    ap.add_argument("--argos", action="store_true", help="also install Argos Translate packs")
    args = ap.parse_args()
    errors = fetch_all(check_only=args.check)
    if args.argos and not args.check:
        errors += install_argos()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
