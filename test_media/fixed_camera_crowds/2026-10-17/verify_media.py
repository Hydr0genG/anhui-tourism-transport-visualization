"""Verify the five fixed-camera MP4 files without network/database access."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["videos"]) == 5
    for item in manifest["videos"]:
        path = (root / item["file"]).resolve()
        assert path.is_relative_to(root), "Video must stay inside this directory"
        assert path.stat().st_size == item["byte_size"], path.name + " size mismatch"
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        assert digest.hexdigest() == item["sha256"], path.name + " SHA256 mismatch"
        print("OK " + path.name)
    print("Verified five fixed-camera video files")


if __name__ == "__main__":
    main()
