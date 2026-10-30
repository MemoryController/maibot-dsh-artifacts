"""Explicit, version-checked installer. Default is read-only verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERLAY = {
    "artifact_handoff.py": HERE / "artifact_handoff.py",
    "artifact_provider.py": HERE / "artifact_provider.py",
    "artifact_safe_files.py": HERE.parents[1] / "safe_files.py",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(root: Path, *, apply: bool = False) -> str:
    root = root.resolve(strict=True)
    spec = json.loads((HERE / "checksums.json").read_text())
    states = []
    for name, hashes in spec["files"].items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or linked Host source: {name}")
        actual = digest(path)
        if actual not in hashes.values():
            raise ValueError(f"Host source differs from pinned v1.3.18: {name}; preserve your changes")
        states.append("before" if actual == hashes["before"] else "after")
    if len(set(states)) != 1:
        raise ValueError("Partial Host patch detected; restore the five original files before retrying")
    destination = root / "src" / "agent"
    if destination.resolve() != destination:
        raise ValueError("Host src/agent must not contain symbolic links")
    for name, source in OVERLAY.items():
        target = destination / name
        if target.is_symlink() or (target.exists() and digest(target) != digest(source)):
            raise ValueError(f"Refusing to overwrite different overlay module: {name}")
    if states[0] == "after":
        if not all((destination / name).is_file() for name in OVERLAY):
            raise ValueError("Partial overlay detected; restore Host sources before retrying")
        return "already_applied"
    # git apply also works in a source tree without .git; never reset/checkout user files.
    subprocess.run(["git", "apply", "--check", str(HERE / "host.patch")], cwd=root, check=True)
    if not apply:
        return "compatible_not_applied"
    created = []
    try:
        for name, source in OVERLAY.items():
            target = destination / name
            if not target.exists():
                with target.open("xb") as output:
                    output.write(source.read_bytes())
                created.append(target)
        subprocess.run(["git", "apply", str(HERE / "host.patch")], cwd=root, check=True)
    except BaseException:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return "applied"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maibot", type=Path, required=True, help="Stopped MaiBot v1.3.18 source directory")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Read-only compatibility check (default)")
    group.add_argument("--apply", action="store_true", help="Explicitly patch this source tree and install modules")
    args = parser.parse_args()
    print(install(args.maibot, apply=args.apply))


if __name__ == "__main__":
    main()
