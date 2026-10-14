"""Format-neutral single-file checks; no index deserialization or model validation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .errors import Conflict, InvalidArgument, NotFound


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _path(data_root: Path | None, relative_path: str) -> Path:
    if data_root is None:
        raise InvalidArgument("索引产物校验必须配置 DATA_ROOT")
    if (not isinstance(relative_path, str) or not relative_path.strip()
            or len(relative_path) > 500 or "\\" in relative_path or "\0" in relative_path):
        raise InvalidArgument("索引产物路径必须为规范相对路径")
    relative = PurePosixPath(relative_path)
    if (relative.is_absolute() or relative.as_posix() != relative_path
            or any(part in {".", ".."} or ":" in part for part in relative.parts)):
        raise InvalidArgument("索引产物路径必须为规范相对路径")
    root = data_root.resolve()
    path = root.joinpath(*relative.parts)
    # Reject all symlinks, including links that currently point inside DATA_ROOT.
    # This also avoids accepting mutable aliases as published artifact references.
    cursor = root
    try:
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise InvalidArgument("索引产物路径不能包含符号链接")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise InvalidArgument("索引产物路径超出 DATA_ROOT")
    except (OSError, RuntimeError) as exc:
        raise Conflict("索引产物路径无法核验") from exc
    return path


@dataclass(frozen=True)
class VerifiedArtifact:
    relative_path: str
    sha256: str
    byte_size: int
    fingerprint: tuple[int, int, int, int, int]

    def ensure_unchanged(self, data_root: Path | None) -> None:
        """Cheap post-lock check, not a filesystem/database atomicity guarantee."""
        path = _path(data_root, self.relative_path)
        try:
            current = path.stat()
        except FileNotFoundError as exc:
            raise NotFound("索引产物不存在") from exc
        except OSError as exc:
            raise Conflict("索引产物无法读取") from exc
        if not stat.S_ISREG(current.st_mode) or _fingerprint(current) != self.fingerprint:
            raise Conflict("索引产物在校验后发生变化")


def verify_artifact(
    data_root: Path | None, relative_path: str, sha256: str,
    expected_byte_size: int | None = None,
) -> VerifiedArtifact:
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise InvalidArgument("索引产物摘要必须为64位小写SHA256")
    if expected_byte_size is not None and (type(expected_byte_size) is not int or expected_byte_size < 0):
        raise InvalidArgument("索引产物预期大小必须为非负整数")
    path = _path(data_root, relative_path)
    try:
        # NONBLOCK avoids hanging on FIFOs; fstat then rejects non-regular files.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InvalidArgument("索引产物必须为普通文件")
            if expected_byte_size is not None and before.st_size != expected_byte_size:
                raise Conflict("索引产物大小与预期不一致")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
    except FileNotFoundError as exc:
        raise NotFound("索引产物不存在") from exc
    except IsADirectoryError as exc:
        raise InvalidArgument("索引产物必须为普通文件") from exc
    except OSError as exc:
        raise Conflict("索引产物无法读取") from exc
    if _fingerprint(before) != _fingerprint(after) or size != after.st_size:
        raise Conflict("索引产物在读取时发生变化")
    if digest.hexdigest() != sha256:
        raise Conflict("索引产物摘要与文件不一致")
    verified = VerifiedArtifact(relative_path, sha256, size, _fingerprint(after))
    verified.ensure_unchanged(data_root)
    return verified
