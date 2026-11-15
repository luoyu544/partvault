from dataclasses import dataclass

from ..models import StockLot, StockMove
from .stock import serialized_write

_EPS = 1e-9
_FIELDS = ("lot_no", "date_code", "unit_price", "supplier_id")
_REPAIR_NOTE = "历史归还批次信息已补全"


@dataclass(frozen=True)
class LegacyReturnReport:
    return_moves: int = 0
    return_lots: int = 0
    missing_lots: int = 0
    repairable_lots: int = 0
    ambiguous_lots: int = 0
    no_source_lots: int = 0

    @property
    def pending_lots(self) -> int:
        return self.repairable_lots + self.ambiguous_lots + self.no_source_lots


def _missing(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _needs_repair(lot: StockLot) -> bool:
    # 供应商是可选信息；只要批次号、日期码、单价齐全，就不必反复提示修复。
    return (
        _missing(lot.lot_no)
        or _missing(lot.date_code)
        or lot.unit_price is None
    )


def _signature(lot: StockLot | None):
    if lot is None:
        return None
    values = (
        (lot.lot_no or "").strip() or None,
        (lot.date_code or "").strip() or None,
        float(lot.unit_price) if lot.unit_price is not None else None,
        int(lot.supplier_id) if lot.supplier_id else None,
    )
    return values if any(value is not None for value in values) else None


def _source_chunks(db, loan_id: int, cache: dict) -> list[tuple[float, float, tuple | None]]:
    if loan_id in cache:
        return cache[loan_id]
    moves = (
        db.query(StockMove)
        .filter(
            StockMove.type == "out",
            StockMove.ref_type == "loan",
            StockMove.ref_id == loan_id,
        )
        .order_by(StockMove.id.asc())
        .all()
    )
    chunks = []
    cursor = 0.0
    for move in moves:
        qty = max(0.0, float(move.qty or 0))
        if qty <= _EPS:
            continue
        source_lot = db.get(StockLot, move.lot_id) if move.lot_id else None
        chunks.append((cursor, cursor + qty, _signature(source_lot)))
        cursor += qty
    cache[loan_id] = chunks
    return chunks


def _return_offsets(db, loan_id: int, cache: dict) -> dict[int, float]:
    if loan_id in cache:
        return cache[loan_id]
    moves = (
        db.query(StockMove)
        .filter(
            StockMove.type == "in",
            StockMove.ref_type == "return",
            StockMove.ref_id == loan_id,
        )
        .order_by(StockMove.id.asc())
        .all()
    )
    offsets = {}
    cursor = 0.0
    for move in moves:
        offsets[move.id] = cursor
        cursor += max(0.0, float(move.qty or 0))
    cache[loan_id] = offsets
    return offsets


def _move_signatures(db, move: StockMove, chunk_cache: dict, offset_cache: dict):
    """返回该归还流水对应的原始批次签名；None 表示没有可追溯的借出流水。"""
    if not move.ref_id:
        return None
    chunks = _source_chunks(db, move.ref_id, chunk_cache)
    if not chunks:
        return None
    start = _return_offsets(db, move.ref_id, offset_cache).get(move.id, 0.0)
    end = start + max(0.0, float(move.qty or 0))
    signatures = []
    covered = 0.0
    for chunk_start, chunk_end, signature in chunks:
        overlap = min(end, chunk_end) - max(start, chunk_start)
        if overlap > _EPS:
            covered += overlap
            if signature is not None and signature not in signatures:
                signatures.append(signature)
    if covered + _EPS < (end - start):
        return None
    return signatures


def _fillable_fields(lot: StockLot, signatures) -> tuple[dict, bool]:
    fillable = {}
    conflict = False
    for index, field in enumerate(_FIELDS):
        if not _missing(getattr(lot, field)):
            continue
        values = {
            signature[index]
            for signature in signatures
            if signature[index] is not None
        }
        if len(values) == 1:
            fillable[field] = values.pop()
        elif len(values) > 1:
            conflict = True
    return fillable, conflict


def _analyze(db) -> tuple[LegacyReturnReport, dict[int, dict]]:
    moves = (
        db.query(StockMove)
        .filter(StockMove.type == "in", StockMove.ref_type == "return")
        .order_by(StockMove.id.asc())
        .all()
    )
    chunk_cache: dict = {}
    offset_cache: dict = {}
    by_lot: dict[int, list[StockMove]] = {}
    for move in moves:
        if move.lot_id:
            by_lot.setdefault(int(move.lot_id), []).append(move)

    details: dict[int, dict] = {}
    for lot_id, lot_moves in by_lot.items():
        lot = db.get(StockLot, lot_id)
        if lot is None or not _needs_repair(lot):
            continue
        signatures = []
        ambiguous = False
        for move in lot_moves:
            found = _move_signatures(db, move, chunk_cache, offset_cache)
            if found is None:
                continue
            if len(found) > 1:
                ambiguous = True
            signatures.extend(signature for signature in found if signature not in signatures)
        if signatures and not ambiguous:
            fillable, conflict = _fillable_fields(lot, signatures)
        else:
            fillable, conflict = {}, bool(ambiguous)
        if fillable:
            status = "repairable"
        elif conflict:
            status = "ambiguous"
        else:
            status = "no_source"
        details[lot.id] = {
            "lot": lot,
            "moves": lot_moves,
            "signatures": signatures,
            "fillable": fillable,
            "status": status,
        }

    report = LegacyReturnReport(
        return_moves=len(moves),
        return_lots=len(by_lot),
        missing_lots=len(details),
        repairable_lots=sum(1 for row in details.values() if row["status"] == "repairable"),
        ambiguous_lots=sum(1 for row in details.values() if row["status"] == "ambiguous"),
        no_source_lots=sum(1 for row in details.values() if row["status"] == "no_source"),
    )
    return report, details


def legacy_return_report(db) -> LegacyReturnReport:
    report, _details = _analyze(db)
    return report


def _append_note(note: str | None) -> str:
    value = (note or "").strip()
    if _REPAIR_NOTE in value:
        return value
    return f"{value}；{_REPAIR_NOTE}" if value else _REPAIR_NOTE


@serialized_write
def repair_legacy_returns(db) -> dict:
    """补全仍能追溯到原始借出批次的旧归还记录。"""
    _report, details = _analyze(db)
    repaired = 0
    fields_filled = 0
    try:
        for row in details.values():
            if row["status"] != "repairable":
                continue
            lot: StockLot = row["lot"]
            for field, value in row["fillable"].items():
                setattr(lot, field, value)
                fields_filled += 1
            price = lot.unit_price
            for move in row["moves"]:
                if move.unit_price is None and price is not None:
                    move.unit_price = price
                move.note = _append_note(move.note)
            repaired += 1
        if repaired:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return {"repaired_lots": repaired, "fields_filled": fields_filled}
