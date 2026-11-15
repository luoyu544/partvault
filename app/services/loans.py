from datetime import datetime

from sqlalchemy import func

from ..models import Loan, Part, StockLot, StockMove
from .stock import serialized_write, stock_in, stock_out


def open_loans(db, part_id: int | None = None, limit: int | None = None):
    query = db.query(Loan).filter(Loan.status == "open", Loan.returned_qty < Loan.qty)
    if part_id:
        query = query.filter(Loan.part_id == part_id)
    query = query.order_by(Loan.created_at.asc(), Loan.id.asc())
    if limit:
        query = query.limit(limit)
    return query.all()


def open_loan_qty(db, part_id: int | None = None) -> float:
    query = db.query(func.coalesce(func.sum(Loan.qty - Loan.returned_qty), 0)) \
        .filter(Loan.status == "open", Loan.returned_qty < Loan.qty)
    if part_id:
        query = query.filter(Loan.part_id == part_id)
    return float(query.scalar() or 0)


def open_loan_count(db) -> int:
    return int(db.query(Loan).filter(Loan.status == "open", Loan.returned_qty < Loan.qty).count())


@serialized_write
def borrow(db, part_id: int, location_id: int | None, qty: float,
           borrower: str = "", note: str = "") -> Loan:
    if qty is None or float(qty) <= 0:
        raise ValueError("数量必须大于 0")
    if not location_id:
        raise ValueError("请扫描借出库位")
    if not db.get(Part, part_id):
        raise ValueError("物料不存在")

    loan = Loan(
        part_id=part_id,
        qty=float(qty),
        returned_qty=0,
        out_location_id=location_id,
        borrower=(borrower or "").strip()[:120],
        note=(note or "").strip(),
        status="open",
        created_at=datetime.now(),
    )
    db.add(loan)
    db.flush()
    move_note = "借出" + (f"给 {borrower.strip()}" if borrower and borrower.strip() else "")
    if note.strip():
        move_note += f"：{note.strip()}"
    try:
        stock_out(db, part_id, float(qty), location_id=location_id, note=move_note,
                  ref_type="loan", ref_id=loan.id)
    except Exception:
        db.rollback()
        raise
    return loan


def _return_chunks(db, loan: Loan, take: float, already_returned: float):
    """按借出流水把归还数量还原到原批次。

    借出时每个批次都会留下一条 StockMove。归还时优先恢复原批次号、日期码和单价；
    如果历史数据没有借出流水，则返回 None 批次，由调用方按普通入库处理。
    """
    moves = (
        db.query(StockMove)
        .filter(
            StockMove.part_id == loan.part_id,
            StockMove.ref_type == "loan",
            StockMove.ref_id == loan.id,
            StockMove.type == "out",
        )
        .order_by(StockMove.id.asc())
        .all()
    )
    chunks = []
    skip = max(0.0, float(already_returned or 0))
    remaining = float(take)
    for move in moves:
        if remaining <= 1e-9:
            break
        move_qty = max(0.0, float(move.qty or 0))
        skipped = min(move_qty, skip)
        skip -= skipped
        available = move_qty - skipped
        if available <= 1e-9:
            continue
        use = min(available, remaining)
        lot = db.get(StockLot, move.lot_id) if move.lot_id else None
        chunks.append((lot, use))
        remaining -= use
    if remaining > 1e-9:
        chunks.append((None, remaining))
    return chunks


@serialized_write
def return_loans(db, part_id: int, location_id: int | None, qty: float,
                 note: str = "") -> float:
    if qty is None or float(qty) <= 0:
        raise ValueError("数量必须大于 0")
    if not location_id:
        raise ValueError("请扫描归还库位")
    if not db.get(Part, part_id):
        raise ValueError("物料不存在")

    items = open_loans(db, part_id=part_id)
    outstanding = sum(float(l.outstanding_qty) for l in items)
    if outstanding <= 1e-9:
        raise ValueError("该物品没有未归还记录")
    if float(qty) > outstanding + 1e-9:
        raise ValueError(f"未归还数量只有 {outstanding:g}，不能归还 {float(qty):g}")

    remaining = float(qty)
    move_note = "归还" + (f"：{note.strip()}" if note and note.strip() else "")
    plans = []
    for loan in items:
        if remaining <= 1e-9:
            break
        take = min(float(loan.outstanding_qty), remaining)
        if take <= 0:
            continue
        previous_returned = float(loan.returned_qty or 0)
        for lot, chunk_qty in _return_chunks(db, loan, take, previous_returned):
            plans.append((loan, lot, chunk_qty))
        loan.returned_qty = previous_returned + take
        loan.return_location_id = location_id
        if loan.outstanding_qty <= 1e-9:
            loan.status = "returned"
            loan.returned_at = datetime.now()
        remaining -= take

    try:
        for loan, lot, chunk_qty in plans:
            chunk_note = move_note
            if lot is not None:
                if lot.lot_no:
                    chunk_note += f"；原批次 {lot.lot_no}"
                if lot.date_code:
                    chunk_note += f"；日期码 {lot.date_code}"
                price = lot.unit_price
                if price is not None and price < 0:
                    price = None
                stock_in(
                    db, part_id, location_id, chunk_qty,
                    lot_no=lot.lot_no or "", date_code=lot.date_code or "",
                    unit_price=price, supplier_id=lot.supplier_id,
                    note=chunk_note, ref_type="return", ref_id=loan.id, commit=False,
                )
            else:
                stock_in(
                    db, part_id, location_id, chunk_qty, note=chunk_note,
                    ref_type="return", ref_id=loan.id, commit=False,
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return float(qty)
