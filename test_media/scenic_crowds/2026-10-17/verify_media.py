"""Verify the five local MP4 test files against the source manifest."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["videos"]:
        path = (root / item["video_file"]).resolve()
        assert path.is_relative_to(root), "Video path must stay inside this package"
        assert path.stat().st_size == item["video_bytes"], path.name + " size mismatch"
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        assert digest.hexdigest() == item["video_sha256"], path.name + " SHA256 mismatch"
        print("OK " + path.name)
    print("Verified " + str(len(manifest["videos"])) + " videos; no database or network access")


if __name__ == "__main__":
    main()
