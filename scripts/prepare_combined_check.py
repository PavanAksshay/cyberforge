"""Export B into a fresh temporary directory and overlay only A-owned integration files.

No git merge, branch switch, shared migration registration edit or database mutation.
Run the printed checkout's tests with the existing virtual environment and its src
on PYTHONPATH. The temporary tree is retained for inspection, never auto-deleted.
"""

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def prepare(b_ref: str) -> Path:
    root = Path(__file__).resolve().parents[1]
    b_commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", b_ref], cwd=root, text=True
    ).strip()
    archive = subprocess.check_output(["git", "archive", "--format=zip", b_commit], cwd=root)
    target = Path(tempfile.mkdtemp(prefix="astraroll-combined-"))
    with zipfile.ZipFile(io.BytesIO(archive)) as snapshot:
        for entry in snapshot.infolist():
            resolved = (target / entry.filename).resolve()
            if not resolved.is_relative_to(target.resolve()):
                raise ValueError("Archive contains a path outside the temporary checkout")
        snapshot.extractall(target)
    files = [
        root / "src/astraroll/api.py",
        root / "src/astraroll/config.py",
        root / "README.md",
        root / "docs/analytics.md",
        root / ".env.example",
        *sorted((root / "src/astraroll/analytics").rglob("*.py")),
        *sorted((root / "tests").glob("test_analytics*.py")),
        root / "tests/analytics_fake.py",
    ]
    hashes = {}
    for source in files:
        relative = source.relative_to(root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        hashes[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "b_commit": b_commit,
        "a_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "overlay_sha256": hashes,
    }
    (target / "combined-check-manifest.json").write_text(json.dumps(manifest, indent=2))
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b-ref", default="origin/dev-b")
    print(prepare(parser.parse_args().b_ref))
