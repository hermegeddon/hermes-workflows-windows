"""Copy the complete plugin without symlink privileges or package dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def install(destination: Path | None = None, *, update: bool = False) -> Path:
    source = Path(__file__).resolve().parent
    target = (
        (destination or Path.home() / ".hermes" / "plugins" / "hermes-workflows")
        .expanduser()
        .resolve()
    )
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("installation destination must be outside the source directory")
    if target.exists() and not update:
        raise FileExistsError(
            "plugin directory already exists; stop Hermes and pass --update to overwrite plugin files"
        )
    target.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py", "README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source / name, target / name)
    for name in ("hermes_workflows", "dashboard", "desktop", "examples"):
        shutil.copytree(
            source / name,
            target / name,
            dirs_exist_ok=update,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    metadata = json.loads((target / "dashboard" / "upstream.json").read_text(encoding="utf-8"))
    for name, expected in {**metadata["files"], **metadata.get("local_files", {})}.items():
        if hashlib.sha256((target / "dashboard" / name).read_bytes()).hexdigest() != expected:
            raise OSError(f"dashboard asset failed verification: {name}")
    desktop = json.loads((target / "desktop" / "assets.json").read_text(encoding="utf-8"))
    for name, expected in desktop["files"].items():
        if hashlib.sha256((target / "desktop" / name).read_bytes()).hexdigest() != expected:
            raise OSError(f"desktop asset failed verification: {name}")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        target = install(args.destination, update=args.update)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Installed hermes-workflows to {target}")


if __name__ == "__main__":
    main()
