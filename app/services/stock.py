import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path

from sqlalchemy import func

from ..models import Location, Part, StockLot, StockMove

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 平台
    msvcrt = None

try:  # Linux / macOS
    import fcntl
except ImportError:  # pragma: no cover - Windows 平台
    fcntl = None

# SQLite 没有行级锁，入库/出库的“先读后写”在并发时可能丢数量。
# 同一个进程内用可重入锁串行化；多个 worker / 多个服务实例之间再用
# 操作系统文件锁串行化。跨主机共享数据库仍应改用 PostgreSQL。
_PROCESS_LOCK = threading.RLock()
_LOCK_STATE = threading.local()
_LOCK_TIMEOUT = 60.0


def _lock_path() -> Path:
    override = os.environ.get("PARTVAULT_WRITE_LOCK")
    if override:
        return Path(override)
    from ..db import DATA_DIR

    return DATA_DIR / ".partvault.write.lock"


def _acquire_file_lock(fd: int) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("库存写入繁忙，请稍后重试") from exc
            time.sleep(0.05)


def _release_file_lock(fd: int) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _write_guard():
    """线程内可重入、进程间互斥的库存写锁。"""
    _PROCESS_LOCK.acquire()
    try:
        depth = int(getattr(_LOCK_STATE, "depth", 0) or 0)
        if depth:
            _LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = depth
            return

        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            _acquire_file_lock(fd)
            locked = True
            _LOCK_STATE.depth = 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = 0
        finally:
            if locked:
                _release_file_lock(fd)
            os.close(fd)
    finally:
        _PROCESS_LOCK.release()


def serialized_write(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _write_guard():
            return fn(*args, **kwargs)

    return wrapper


def next_part_code(db) -> str:
    max_id = db.query(func.max(Part.id)).scalar() or 0
    return f"PV-P-{max_id + 1:06d}"


def part_total_qty(db, part_id: int) -> float:
    q = db.query(func.coalesce(func.sum(StockLot.qty), 0)).filter(StockLot.part_id == part_id).scalar()
    return float(q or 0)


def location_path(db, loc: Location | None) -> str:
    if not loc:
        return ""
    names = []
    cur = loc
    seen = set()
    while cur and cur.id not in seen:
        seen.add(cur.id)
        names.append(cur.code or cur.name)
        cur = db.get(Location, cur.parent_id) if cur.parent_id else None
    return " / ".join(reversed(names))


@serialized_write
def stock_in(
    db,
    part_id: int,
    location_id: int | None,
    qty: float,
    lot_no: str = "",
    date_code: str = "",
    unit_price: float | None = None,
    supplier_id: int | None = None,
    note: str = "",
    ref_type: str = "manual",
    ref_id: int | None = None,
    commit: bool = True,
) -> StockLot:
    if qty is None or qty <= 0:
        raise ValueError("数量必须大于 0")
    if unit_price is not None and unit_price < 0:
        raise ValueError("单价不能为负数")
    part = db.get(Part, part_id)
    if not part:
        raise ValueError("物料不存在")
    if location_id is None:
        location_id = part.default_location_id
    if location_id is None:
        raise ValueError("请选择库位")
    if db.get(Location, location_id) is None:
        raise ValueError("库位不存在，请重新选择")

    lot = (
        db.query(StockLot)
        .filter(
            StockLot.part_id == part_id,
            StockLot.location_id == location_id,
            func.coalesce(StockLot.lot_no, "") == (lot_no or ""),
            func.coalesce(StockLot.date_code, "") == (date_code or ""),
        )
        .first()
    )
    if lot is None:
        lot = StockLot(
            part_id=part_id,
            location_id=location_id,
            qty=0,
            lot_no=lot_no or None,
            date_code=date_code or None,
            unit_price=unit_price,
            supplier_id=supplier_id,
            received_at=datetime.now(),
        )
        db.add(lot)
        db.flush()
    lot.qty = float(lot.qty or 0) + float(qty)
    if unit_price is not None:
        lot.unit_price = unit_price
    if supplier_id:
        lot.supplier_id = supplier_id

    db.add(StockMove(
        part_id=part_id,
        lot_id=lot.id,
        type="in",
        qty=float(qty),
        to_location_id=location_id,
        unit_price=unit_price,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note or None,
        created_at=datetime.now(),
    ))
    if commit:
        db.commit()
    else:
        db.flush()
    return lot


@serialized_write
def stock_out(
    db,
    part_id: int,
    qty: float,
    location_id: int | None = None,
    note: str = "",
    ref_type: str = "manual",
    ref_id: int | None = None,
) -> float:
    if qty is None or qty <= 0:
        raise ValueError("数量必须大于 0")
    query = db.query(StockLot).filter(StockLot.part_id == part_id, StockLot.qty > 0)
    if location_id:
        query = query.filter(StockLot.location_id == location_id)
    lots = query.order_by(StockLot.received_at.asc(), StockLot.id.asc()).all()
    available = sum(float(l.qty or 0) for l in lots)
    if available + 1e-9 < float(qty):
        raise ValueError(f"库存不足，当前可用 {available:g}")

    remaining = float(qty)
    for lot in lots:
        if remaining <= 1e-9:
            break
        take = min(float(lot.qty or 0), remaining)
        if take <= 0:
            continue
        lot.qty = float(lot.qty or 0) - take
        db.add(StockMove(
            part_id=part_id,
            lot_id=lot.id,
            type="out",
            qty=take,
            from_location_id=lot.location_id,
            unit_price=lot.unit_price,
            ref_type=ref_type,
            ref_id=ref_id,
            note=note or None,
            created_at=datetime.now(),
        ))
        remaining -= take
    db.commit()
    return float(qty)


def _apply_adjustment(db, lot: StockLot, actual_qty: float, note: str = "") -> float:
    """把批次数量改成盘点实数，并写入一条 signed 调整流水。"""
    actual = float(actual_qty)
    if actual < 0:
        raise ValueError("盘点数量不能为负数")
    current = float(lot.qty or 0)
    delta = actual - current
    if abs(delta) <= 1e-9:
        return 0.0
    lot.qty = actual
    db.add(StockMove(
        part_id=lot.part_id,
        lot_id=lot.id,
        type="adjust",
        qty=delta,
        from_location_id=lot.location_id if delta < 0 else None,
        to_location_id=lot.location_id if delta > 0 else None,
        unit_price=lot.unit_price,
        ref_type="stocktake",
        note=(note or "").strip() or "盘点调整",
        created_at=datetime.now(),
    ))
    return delta


@serialized_write
def adjust_stock(db, lot_id: int, actual_qty: float, note: str = "") -> float:
    """调整单个批次数量，返回增减量（正数盘盈、负数盘亏）。"""
    lot = db.get(StockLot, lot_id)
    if lot is None:
        raise ValueError("批次不存在，请刷新后重试")
    try:
        delta = _apply_adjustment(db, lot, actual_qty, note)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return delta


@serialized_write
def apply_stocktake(db, entries, note: str = "") -> dict:
    """原子提交盘点结果。

    ``entries`` 为 ``[(lot_id, actual_qty), ...]``。只对数量发生变化的批次
    写 stock_moves，全部成功才提交，避免盘点只改了一半。
    """
    normalized: list[tuple[int, float]] = []
    seen: set[int] = set()
    for raw_lot_id, raw_actual in entries:
        try:
            lot_id = int(str(raw_lot_id).strip())
            actual = float(str(raw_actual).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("盘点数量格式不正确") from exc
        if lot_id <= 0:
            raise ValueError("盘点批次无效")
        if lot_id in seen:
            raise ValueError("同一批次不能重复提交")
        if actual < 0:
            raise ValueError("盘点数量不能为负数")
        seen.add(lot_id)
        normalized.append((lot_id, actual))

    if not normalized:
        raise ValueError("没有可提交的盘点数据")

    changed: list[tuple[StockLot, float]] = []
    for lot_id, actual in normalized:
        lot = db.get(StockLot, lot_id)
        if lot is None:
            db.rollback()
            raise ValueError("部分批次已不存在，请刷新后重新盘点")
        if abs(float(lot.qty or 0) - actual) > 1e-9:
            changed.append((lot, actual))

    if not changed:
        db.rollback()
        return {"count": 0, "delta": 0.0, "surplus": 0, "shortage": 0}

    delta_total = 0.0
    surplus = 0
    shortage = 0
    try:
        for lot, actual in changed:
            delta = _apply_adjustment(db, lot, actual, note)
            delta_total += delta
            if delta > 0:
                surplus += 1
            elif delta < 0:
                shortage += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "count": len(changed),
        "delta": delta_total,
        "surplus": surplus,
        "shortage": shortage,
    }
