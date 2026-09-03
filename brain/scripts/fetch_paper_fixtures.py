#!/usr/bin/env python3
"""Fetch checksum-pinned arXiv source fixtures for local corpus tests.

The archives are intentionally excluded from Git until their redistribution
licenses have been reviewed. Downloads are written atomically and accepted only
when both the declared byte length and SHA-256 digest match the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_DIR = ROOT / "tests" / "fixtures" / "papers" / "manifests"


def digest(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    return size, hasher.hexdigest()


def fetch(manifest_path: Path, *, force: bool) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    destination = (manifest_path.parent / manifest["source_file"]).resolve()
    expected_size = int(manifest["bytes"])
    expected_digest = str(manifest["sha256"])

    if destination.is_file() and not force:
        size, actual_digest = digest(destination)
        if (size, actual_digest) == (expected_size, expected_digest):
            return f"verified {destination.name}"
        raise RuntimeError(
            f"existing fixture failed verification: {destination} "
            f"(bytes={size}, sha256={actual_digest})"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        manifest["source_url"],
        headers={"User-Agent": "marginalia-research-brain/0.1 fixture-fetcher"},
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            with urlopen(request, timeout=120) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
        temporary_path = Path(temporary_name)
        size, actual_digest = digest(temporary_path)
        if (size, actual_digest) != (expected_size, expected_digest):
            raise RuntimeError(
                f"download failed verification for {manifest_path.name}: "
                f"expected bytes={expected_size}, sha256={expected_digest}; "
                f"got bytes={size}, sha256={actual_digest}"
            )
        temporary_path.replace(destination)
        temporary_name = None
        return f"fetched and verified {destination.name}"
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    manifests = sorted(args.manifest_dir.glob("*.json"))
    if not manifests:
        parser.error(f"no manifests found under {args.manifest_dir}")
    for manifest in manifests:
        print(fetch(manifest, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
