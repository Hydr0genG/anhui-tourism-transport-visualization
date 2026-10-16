from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import BinaryIO
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .errors import InvalidArgument
from .errors import Conflict
from .artifacts import verify_artifact
from .repositories import UnitOfWork


class DataStore:
    def __init__(
        self, database_url: str, *, data_root: str | Path | None = None,
        allow_sqlite_for_tests: bool = False,
    ) -> None:
        if database_url.startswith("sqlite") and not allow_sqlite_for_tests:
            raise InvalidArgument("正式运行必须显式使用 MySQL；SQLite 只允许测试")
        if not database_url.startswith(("mysql+pymysql://", "sqlite")):
            raise InvalidArgument("DATABASE_URL 必须使用 mysql+pymysql 驱动")
        self.database_url = database_url
        self.data_root = Path(data_root).resolve() if data_root else None
        self.engine: Engine = create_engine(database_url, pool_pre_ping=True)
        if database_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
                # SQLite legacy mode does not start a transaction for SAVEPOINT.
                # Let SQLAlchemy explicitly begin so nested batch rollback remains
                # subordinate to the caller's outer transaction.
                dbapi_connection.isolation_level = None
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
            @event.listens_for(self.engine, "begin")
            def _sqlite_begin(connection) -> None:  # type: ignore[no-untyped-def]
                connection.exec_driver_sql("BEGIN")
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def read(self) -> Iterator[UnitOfWork]:
        with self._sessions() as session:
            try:
                yield UnitOfWork(session, False, self.data_root)
            finally:
                session.rollback()

    @contextmanager
    def write(self) -> Iterator[UnitOfWork]:
        with self._sessions() as session:
            try:
                yield UnitOfWork(session, True, self.data_root)
                session.commit()
            except Exception:
                session.rollback()
                raise

    def doctor(self) -> dict[str, str]:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"database": "ok", "dialect": self.engine.dialect.name}

    def _verified_input_ref(self, metadata):
        verified = verify_artifact(self.data_root, metadata["relative_path"], metadata["sha256"], metadata["byte_size"])
        return verified, {**metadata, "file_path": str(self.data_root / verified.relative_path)}

    def get_document_file_ref(self, document_id: str) -> dict:
        """Internal only; D must authorize the document and hide paths from HTTP."""
        def metadata():
            with self.read() as uow:
                document = uow.knowledge.get_document(document_id)
                asset = uow.provenance.get_asset(document["asset_id"], internal=True)
                if asset["source_id"] != document["source_id"]:
                    raise Conflict("原文来源与文档登记不一致")
                return {"document_id": document_id, "document_version": document["document_version"],
                        "asset_id": asset["id"], "source_id": asset["source_id"], "relative_path": asset["relative_path"],
                        "sha256": asset["sha256"], "byte_size": asset["byte_size"], "media_type": asset["media_type"]}
        before = metadata()
        verified, reference = self._verified_input_ref(before)  # File IO outside DB transactions.
        if metadata() != before:
            raise Conflict("知识原文登记在取回时发生变化")
        verified.ensure_unchanged(self.data_root)
        return reference

    def get_visible_input(self, analysis_id, lease, *, now=None) -> dict:
        """Authorize analysis, hash outside locks, then recheck lease and permission.

        Not a playback grant. Returned file_path is only for D/E worker internals;
        a verified reference is not a filesystem lock for later decoding.
        """
        with self.write() as uow:
            before = uow.visible.input_metadata(analysis_id, lease, now)
        verified, reference = self._verified_input_ref(before)
        with self.write() as uow:
            if uow.visible.input_metadata(analysis_id, lease, now) != before:
                raise Conflict("录像输入登记在校验时发生变化")
            verified.ensure_unchanged(self.data_root)
        return reference

    def persist_file(
        self, *, source_id: str, original_name: str, stream: BinaryIO,
    ) -> dict[str, str | int]:
        """先将原件完整落盘，再由调用方在事务中登记 source_asset。"""
        if self.data_root is None:
            raise InvalidArgument("写入原件前必须配置 DATA_ROOT")
        if not re.fullmatch(r"[0-9A-Za-z-]{1,64}", source_id):
            raise InvalidArgument("source_id 不能用于安全路径")
        suffix = Path(original_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix or ""):
            suffix = ".bin"
        staging = self.data_root / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as handle:
            temporary = Path(handle.name)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        relative = Path("raw") / source_id / sha256 / f"original{suffix}"
        destination = self.data_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temporary.unlink()
        else:
            os.replace(temporary, destination)
        return {"relative_path": relative.as_posix(), "sha256": sha256, "byte_size": size}

    def dispose(self) -> None:
        self.engine.dispose()


def create_store(
    database_url: str, *, data_root: str | Path | None = None,
    allow_sqlite_for_tests: bool = False,
) -> DataStore:
    return DataStore(
        database_url, data_root=data_root, allow_sqlite_for_tests=allow_sqlite_for_tests
    )
