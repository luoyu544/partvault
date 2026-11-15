from sqlalchemy import func, or_, select

from ..models import BomItem, Part, PartSource


class BomError(ValueError):
    """可直接展示给用户的 BOM 操作错误。"""


def find_part(db, query: str) -> Part | None:
    """按编码、供应商料号、型号或名称查找一个物料。"""
    text = (query or "").strip()
    if not text:
        return None
    low = text.lower()

    part = db.query(Part).filter(func.lower(Part.code) == low).first()
    if part:
        return part

    part = db.query(Part).filter(func.lower(Part.supplier_part_no) == low).first()
    if part:
        return part

    source = db.query(PartSource).filter(func.lower(PartSource.supplier_part_no) == low).first()
    if source:
        part = db.get(Part, source.part_id)
        if part:
            return part

    part = db.query(Part).filter(func.lower(Part.model) == low).first()
    if part:
        return part

    part = db.query(Part).filter(Part.model.ilike(text)).order_by(Part.id.asc()).first()
    if part:
        return part

    part = db.query(Part).filter(Part.model.ilike(f"%{text}%")).order_by(Part.id.asc()).first()
    if part:
        return part

    source_ids = select(PartSource.part_id).where(PartSource.supplier_part_no.ilike(f"%{text}%"))
    return (
        db.query(Part)
        .filter(or_(Part.code.ilike(f"%{text}%"), Part.name.ilike(f"%{text}%"), Part.id.in_(source_ids)))
        .order_by(Part.id.asc())
        .first()
    )


def _get_item(db, project_id: int, item_id: int) -> BomItem:
    item = (
        db.query(BomItem)
        .filter(BomItem.id == item_id, BomItem.project_id == project_id)
        .first()
    )
    if item is None:
        raise BomError("BOM 行不存在，请刷新后重试")
    return item


def assign_item(
    db,
    project_id: int,
    item_id: int,
    part_query: str = "",
    part_id: int | None = None,
) -> BomItem:
    """把 BOM 行手动指定到物料；未匹配行和已匹配行都可使用。"""
    item = _get_item(db, project_id, item_id)
    part = db.get(Part, int(part_id)) if part_id else None
    if part is None:
        part = find_part(db, part_query)
    if part is None:
        shown = (part_query or "").strip() or str(part_id or "")
        raise BomError(f"没有找到物料「{shown}」，请输入完整编码、型号或供应商料号")
    item.part_id = part.id
    item.matched = True
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise BomError("保存匹配关系失败，请重试") from exc
    return item


def clear_item(db, project_id: int, item_id: int) -> BomItem:
    item = _get_item(db, project_id, item_id)
    item.part_id = None
    item.matched = False
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise BomError("取消匹配失败，请重试") from exc
    return item
