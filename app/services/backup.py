import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from .. import db as db_module
from ..db import BACKUP_DIR, DB_PATH, UPLOAD_DIR
from .stock import serialized_write

MAX_BACKUP_BYTES = 512 * 1024 * 1024
REQUIRED_TABLES = {"parts", "locations", "stock_lots", "stock_moves"}
COUNT_TABLES = ("parts", "locations", "stock_lots", "stock_moves", "loans")


class BackupError(ValueError):
    """可直接展示给用户的备份/恢复错误。"""


def _paths(db_path=None, upload_dir=None, backup_dir=None):
    return (
        Path(db_path or DB_PATH),
        Path(upload_dir or UPLOAD_DIR),
        Path(backup_dir or BACKUP_DIR),
    )


def _new_backup_path(backup_dir: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = backup_dir / f"{prefix}_{stamp}.zip"
    index = 1
    while candidate.exists():
        candidate = backup_dir / f"{prefix}_{stamp}_{index}.zip"
        index += 1
    return candidate


def create_backup(db_path=None, upload_dir=None, backup_dir=None, prefix: str = "partvault_backup") -> Path:
    """用 SQLite 在线备份 API 生成一致快照，再打包上传图片。"""
    db_path, upload_dir, backup_dir = _paths(db_path, upload_dir, backup_dir)
    if not db_path.exists():
        raise BackupError("数据库文件不存在，无法备份")
    backup_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".backup_", dir=str(backup_dir)))
    snapshot = temp_dir / "partvault.db"
    dest = _new_backup_path(backup_dir, prefix)
    try:
        source = sqlite3.connect(str(db_path), timeout=30)
        target = sqlite3.connect(str(snapshot))
        try:
            source.execute("PRAGMA busy_timeout=30000")
            source.backup(target)
        finally:
            target.close()
            source.close()

        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, "partvault.db")
            if upload_dir.exists():
                for file in upload_dir.rglob("*"):
                    if not file.is_file():
                        continue
                    rel = file.relative_to(upload_dir).as_posix()
                    zf.write(file, f"uploads/{rel}")
        return dest
    except Exception as exc:
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise BackupError(f"备份失败：{exc}") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _safe_member(name: str) -> PurePosixPath:
    normalized = (name or "").replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise BackupError("备份文件包含不安全的路径")
    if path.parts and ":" in path.parts[0]:
        raise BackupError("备份文件包含不安全的路径")
    return path


def _inspect_db(path: Path) -> dict:
    try:
        conn = sqlite3.connect(str(path), timeout=15)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise BackupError("备份数据库完整性检查未通过")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = REQUIRED_TABLES - tables
            if missing:
                raise BackupError(
                    "备份数据库缺少必要数据表：" + "、".join(sorted(missing))
                )
            counts = {}
            for table in COUNT_TABLES:
                if table in tables:
                    counts[table] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
            return {"tables": tables, "counts": counts}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"备份数据库无法读取：{exc}") from exc


def inspect_archive(archive_path: str | Path) -> dict:
    path = Path(archive_path)
    if not path.exists():
        raise BackupError("备份文件不存在")
    if path.stat().st_size > MAX_BACKUP_BYTES:
        raise BackupError("备份文件过大（最大 512 MB）")
    try:
        with zipfile.ZipFile(path) as zf:
            names = []
            has_db = False
            has_uploads = False
            for info in zf.infolist():
                member = _safe_member(info.filename)
                names.append(member)
                if member.as_posix() == "partvault.db" and not info.is_dir():
                    has_db = True
                if member.parts and member.parts[0] == "uploads" and not info.is_dir():
                    has_uploads = True
            if not has_db:
                raise BackupError("备份中缺少 partvault.db")
            with tempfile.TemporaryDirectory(prefix="pv_inspect_") as tmp:
                target = Path(tmp) / "partvault.db"
                with zf.open("partvault.db") as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output)
                info = _inspect_db(target)
            info.update({"has_uploads": has_uploads, "size": path.stat().st_size})
            return info
    except zipfile.BadZipFile as exc:
        raise BackupError("不是有效的 zip 备份文件") from exc


def _extract_uploads(zf: zipfile.ZipFile, target_dir: Path) -> bool:
    target_dir.mkdir(parents=True, exist_ok=True)
    found = False
    for info in zf.infolist():
        member = _safe_member(info.filename)
        if not member.parts or member.parts[0] != "uploads" or info.is_dir():
            continue
        relative = PurePosixPath(*member.parts[1:])
        if not relative.parts:
            continue
        destination = target_dir / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, open(destination, "wb") as output:
            shutil.copyfileobj(source, output)
        found = True
    return found


def _replace_uploads(temp_uploads: Path, upload_dir: Path, backup_dir: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old = upload_dir.with_name(f"{upload_dir.name}.old_{stamp}")
    index = 1
    while old.exists():
        old = upload_dir.with_name(f"{upload_dir.name}.old_{stamp}_{index}")
        index += 1
    moved_old = False
    try:
        if upload_dir.exists():
            upload_dir.rename(old)
            moved_old = True
        shutil.move(str(temp_uploads), str(upload_dir))
    except Exception:
        if upload_dir.exists() and not moved_old:
            shutil.rmtree(upload_dir, ignore_errors=True)
        if moved_old and old.exists() and not upload_dir.exists():
            old.rename(upload_dir)
        raise
    finally:
        if moved_old and old.exists():
            shutil.rmtree(old, ignore_errors=True)


@serialized_write
def restore_archive(
    archive_path: str | Path,
    db_path=None,
    upload_dir=None,
    backup_dir=None,
    create_safety: bool = True,
) -> dict:
    """恢复完整备份；恢复前自动生成当前数据的安全备份。"""
    path = Path(archive_path)
    db_path, upload_dir, backup_dir = _paths(db_path, upload_dir, backup_dir)
    info = inspect_archive(path)
    live = db_path.resolve() == DB_PATH.resolve()
    safety = None
    if create_safety:
        safety = create_backup(db_path=db_path, upload_dir=upload_dir, backup_dir=backup_dir,
                               prefix="partvault_before_restore")

    with tempfile.TemporaryDirectory(prefix="pv_restore_") as tmp:
        tmp_dir = Path(tmp)
        restored_db = tmp_dir / "partvault.db"
        try:
            with zipfile.ZipFile(path) as zf:
                with zf.open("partvault.db") as source, open(restored_db, "wb") as output:
                    shutil.copyfileobj(source, output)
                temp_uploads = tmp_dir / "uploads"
                has_uploads = _extract_uploads(zf, temp_uploads)
        except zipfile.BadZipFile as exc:
            raise BackupError("不是有效的 zip 备份文件") from exc

        if live:
            db_module.engine.dispose()

        try:
            target = sqlite3.connect(str(db_path), timeout=30)
            try:
                target.execute("PRAGMA busy_timeout=30000")
                try:
                    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
                source = sqlite3.connect(str(restored_db), timeout=30)
                try:
                    source.backup(target)
                finally:
                    source.close()
                try:
                    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
            finally:
                target.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"写入恢复数据失败：{exc}") from exc
        finally:
            if live:
                db_module.engine.dispose()

        if has_uploads:
            try:
                _replace_uploads(temp_uploads, upload_dir, backup_dir)
            except OSError as exc:
                raise BackupError(f"恢复上传图片失败：{exc}") from exc

    return {
        "counts": info.get("counts", {}),
        "has_uploads": info.get("has_uploads", False),
        "safety_backup": safety.name if safety else "",
    }


def list_backups(backup_dir=None, limit: int = 20) -> list[dict]:
    directory = Path(backup_dir or BACKUP_DIR)
    if not directory.exists():
        return []
    rows = []
    for file in directory.iterdir():
        if not file.is_file() or file.suffix.lower() != ".zip" or file.name.startswith("."):
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        rows.append({
            "name": file.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime),
        })
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:max(1, int(limit or 20))]


def resolve_backup(name: str, backup_dir=None) -> Path:
    directory = Path(backup_dir or BACKUP_DIR).resolve()
    if not name or Path(name).name != name:
        raise BackupError("备份文件名无效")
    path = (directory / name).resolve()
    if path.parent != directory or path.suffix.lower() != ".zip":
        raise BackupError("备份文件名无效")
    if not path.is_file():
        raise BackupError("备份文件不存在")
    return path
