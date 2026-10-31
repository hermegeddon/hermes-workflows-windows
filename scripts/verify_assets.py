"""Check shipped web/Desktop bytes without rebuilding or changing manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

REQUIRED = {
    ("dashboard", "upstream.json", "files"): {"dist/index.js", "dist/index.css"},
    ("dashboard", "upstream.json", "local_files"): {
        "manifest.json",
        "entry.js",
        "style.css",
        "compat/index.js",
        "compat/index.css",
    },
    ("desktop", "assets.json", "files"): {"plugin.js"},
}


def verify(plugin: Path) -> int:
    checked = 0
    for (directory, manifest, group), required in REQUIRED.items():
        root = (plugin / directory).resolve()
        data = json.loads((root / manifest).read_text(encoding="utf-8"))
        records = data.get(group)
        if not isinstance(records, dict) or not required.issubset(records):
            raise ValueError(f"{directory}/{manifest}: missing required {group} records")
        for name, expected in records.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError(f"unsafe asset path: {name}")
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"asset escapes package: {name}")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"invalid SHA-256 for {directory}/{name}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"SHA-256 mismatch: {directory}/{name}")
            checked += 1
    if (plugin / "desktop/plugin.js").stat().st_size > 512 * 1024:
        raise ValueError("Desktop bundle exceeds the host's 512 KiB limit")
    return checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "hermes-workflows-py",
    )
    args = parser.parse_args()
    try:
        count = verify(args.plugin_dir)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Asset verification failed: {exc}\n")
    print(f"Verified {count} web/Desktop asset hashes and Desktop bundle size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
