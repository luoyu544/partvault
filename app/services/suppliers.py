from sqlalchemy import func

from ..models import Part, PartSource, StockLot, Supplier

SUPPLIER_TYPES = ("lcsc", "taobao", "other")
SUPPLIER_TYPE_LABELS = {
    "lcsc": "立创",
    "taobao": "淘宝",
    "other": "其他",
}


class SupplierError(ValueError):
    """可直接展示给用户的供应商校验错误。"""


def normalize_type(value: str, default: str = "other") -> str:
    kind = (value or default).strip().lower()
    if kind not in SUPPLIER_TYPES:
        raise SupplierError("供应商类型无效")
    return kind


def _clean_values(name: str, type: str, homepage: str, note: str):
    name = (name or "").strip()
    if not name:
        raise SupplierError("供应商名不能为空")
    if len(name) > 120:
        raise SupplierError("供应商名最多 120 个字符")
    kind = normalize_type(type)
    homepage = (homepage or "").strip()
    if len(homepage) > 300:
        raise SupplierError("主页地址最多 300 个字符")
    note = (note or "").strip()
    return name, kind, homepage, note


def _find_same_name(db, name: str, exclude_id: int | None = None) -> Supplier | None:
    query = db.query(Supplier).filter(func.lower(Supplier.name) == name.lower())
    if exclude_id is not None:
        query = query.filter(Supplier.id != exclude_id)
    return query.first()


def create_supplier(db, name: str, type: str = "other", homepage: str = "", note: str = "") -> Supplier:
    name, kind, homepage, note = _clean_values(name, type, homepage, note)
    if _find_same_name(db, name):
        raise SupplierError(f"供应商「{name}」已存在")
    supplier = Supplier(name=name, type=kind, homepage=homepage or None, note=note or None)
    db.add(supplier)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise SupplierError("保存失败，请重试") from exc
    return supplier


def update_supplier(
    db,
    supplier_id: int,
    name: str,
    type: str = "other",
    homepage: str = "",
    note: str = "",
) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierError("供应商不存在")
    name, kind, homepage, note = _clean_values(name, type, homepage, note)
    if _find_same_name(db, name, exclude_id=supplier.id):
        raise SupplierError(f"供应商「{name}」已存在")
    supplier.name = name
    supplier.type = kind
    supplier.homepage = homepage or None
    supplier.note = note or None
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise SupplierError("保存失败，请重试") from exc
    return supplier


def supplier_usage(db, supplier_id: int) -> dict[str, int]:
    return {
        "parts": int(db.query(Part).filter(Part.supplier_id == supplier_id).count()),
        "sources": int(db.query(PartSource).filter(PartSource.supplier_id == supplier_id).count()),
        "lots": int(db.query(StockLot).filter(StockLot.supplier_id == supplier_id).count()),
    }


def delete_supplier(db, supplier_id: int) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierError("供应商不存在")
    usage = supplier_usage(db, supplier.id)
    if any(usage.values()):
        detail = []
        if usage["parts"]:
            detail.append(f"{usage['parts']} 个物料")
        if usage["sources"]:
            detail.append(f"{usage['sources']} 条来源")
        if usage["lots"]:
            detail.append(f"{usage['lots']} 个批次")
        raise SupplierError(
            f"供应商「{supplier.name}」仍被 {'、'.join(detail)} 使用，不能删除"
        )
    try:
        db.delete(supplier)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise SupplierError("删除失败，请重试") from exc
    return supplier
