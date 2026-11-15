import re
from dataclasses import dataclass

from sqlalchemy import func

from ..models import Loan, Location, Part, StockLot, StockMove
from .stock import serialized_write

LOCATION_KINDS = ("warehouse", "cabinet", "shelf", "box", "cell")
LOCATION_KIND_LABELS = {
    "warehouse": "仓库",
    "cabinet": "柜",
    "shelf": "层",
    "box": "盒",
    "cell": "格",
}
MAX_BATCH_LOCATIONS = 200
TEMP_LOCATION_CODE = "TEMP"
TEMP_LOCATION_NAME = "临时库位"
_SEPARATOR_RE = re.compile(r"[\t,，|]")


class LocationError(ValueError):
    """可直接展示给用户的库位校验错误。"""


@dataclass(frozen=True)
class LocationDraft:
    code: str
    name: str
    line_no: int


@dataclass(frozen=True)
class LocationDeleteResult:
    deleted_codes: list[str]
    moved_lot_count: int
    moved_part_count: int
    moved_qty: float
    temp_code: str = ""


@dataclass(frozen=True)
class LocationMoveResult:
    part_count: int
    lot_count: int
    qty: float
    to_code: str


def normalize_kind(kind: str, default: str = "cell") -> str:
    value = (kind or default).strip().lower()
    if value not in LOCATION_KINDS:
        raise LocationError("库位类型无效")
    return value


def _clean_code(code: str) -> str:
    return (code or "").strip()


def _clean_name(name: str, code: str) -> str:
    value = (name or "").strip()
    return value or code


def _is_reserved_code(code: str) -> bool:
    return (code or "").strip().casefold() == TEMP_LOCATION_CODE.casefold()


def _check_lengths(code: str, name: str, prefix: str = "") -> None:
    if not code:
        raise LocationError(f"{prefix}库位编码不能为空")
    if len(code) > 60:
        raise LocationError(f"{prefix}库位编码最多 60 个字符")
    if len(name) > 120:
        raise LocationError(f"{prefix}库位名称最多 120 个字符")


def parse_location_batch(text: str) -> list[LocationDraft]:
    """解析批量库位清单。

    每行一个库位；支持 ``编码,名称``（也接受 Tab、中文逗号、竖线），
    只写编码时名称自动等于编码。空行和以 # 开头的行会忽略。
    """
    drafts: list[LocationDraft] = []
    seen: dict[str, int] = {}

    for line_no, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = _SEPARATOR_RE.split(line, maxsplit=1)
        code = _clean_code(parts[0])
        name = _clean_name(parts[1] if len(parts) > 1 else "", code)
        _check_lengths(code, name, f"第 {line_no} 行：")

        key = code.lower()
        if key in seen:
            raise LocationError(f"第 {line_no} 行：编码 {code} 与第 {seen[key]} 行重复")
        seen[key] = line_no
        drafts.append(LocationDraft(code=code, name=name, line_no=line_no))

        if len(drafts) > MAX_BATCH_LOCATIONS:
            raise LocationError(f"一次最多新建 {MAX_BATCH_LOCATIONS} 个库位")

    return drafts


def _parent_id(parent_id) -> int | None:
    if parent_id in (None, ""):
        return None
    try:
        value = int(parent_id)
    except (TypeError, ValueError) as exc:
        raise LocationError("上级库位无效") from exc
    return value or None


def _validated_parent_id(db, parent_id) -> int | None:
    value = _parent_id(parent_id)
    if value is not None and db.get(Location, value) is None:
        raise LocationError("上级库位不存在")
    return value


def _find_same_code(db, code: str) -> Location | None:
    return (
        db.query(Location)
        .filter(func.lower(Location.code) == code.lower())
        .first()
    )


def _existing_codes(db, codes: list[str]) -> list[str]:
    if not codes:
        return []
    lowered = [code.lower() for code in codes]
    rows = (
        db.query(Location.code)
        .filter(func.lower(Location.code).in_(lowered))
        .all()
    )
    found: dict[str, str] = {}
    for (code,) in rows:
        if code:
            found[code.lower()] = code
    return [found[key] for key in sorted(found)]


def create_location(
    db,
    code: str,
    name: str = "",
    parent_id=None,
    kind: str = "cell",
    note: str = "",
) -> Location:
    code = _clean_code(code)
    name = _clean_name(name, code)
    _check_lengths(code, name)
    if _is_reserved_code(code):
        raise LocationError(f"{TEMP_LOCATION_CODE} 是系统保留的临时库位编码")
    kind = normalize_kind(kind)
    parent_id = _validated_parent_id(db, parent_id)
    if _find_same_code(db, code):
        raise LocationError(f"库位编码 {code} 已存在")

    loc = Location(
        code=code,
        name=name,
        parent_id=parent_id,
        kind=kind,
        note=(note or "").strip() or None,
        sort=0,
    )
    db.add(loc)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocationError("保存失败，请重试") from exc
    return loc


def create_locations_batch(
    db,
    text: str,
    parent_id=None,
    kind: str = "cell",
) -> list[Location]:
    """原子地批量创建库位；任何一行不合法都不会写入数据库。"""
    kind = normalize_kind(kind)
    parent_id = _validated_parent_id(db, parent_id)
    drafts = parse_location_batch(text)
    if not drafts:
        raise LocationError("请至少输入一个库位")
    reserved = [draft for draft in drafts if _is_reserved_code(draft.code)]
    if reserved:
        raise LocationError(
            f"第 {reserved[0].line_no} 行：{TEMP_LOCATION_CODE} 是系统保留的临时库位编码"
        )

    duplicates = _existing_codes(db, [draft.code for draft in drafts])
    if duplicates:
        shown = "、".join(duplicates[:5])
        if len(duplicates) > 5:
            shown += " 等"
        raise LocationError(f"以下编码已存在：{shown}")

    locations = [
        Location(
            code=draft.code,
            name=draft.name,
            parent_id=parent_id,
            kind=kind,
            sort=0,
        )
        for draft in drafts
    ]
    db.add_all(locations)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocationError("保存失败，可能有编码刚被占用，请重试") from exc
    return locations


def update_location(
    db,
    location_id: int,
    code: str,
    name: str = "",
    parent_id=None,
    kind: str = "cell",
    note: str = "",
) -> Location:
    """修改库位属性；临时库位的编码和上级保持锁定。"""
    loc = db.get(Location, location_id)
    if loc is None:
        raise LocationError("库位不存在")

    is_temp = _is_reserved_code(loc.code)
    code = loc.code if is_temp else _clean_code(code)
    name = _clean_name(name, code)
    _check_lengths(code, name)
    kind = normalize_kind(kind)

    if is_temp:
        parent_id = loc.parent_id
    else:
        if _is_reserved_code(code):
            raise LocationError(f"{TEMP_LOCATION_CODE} 是系统保留的临时库位编码")
        parent_id = _validated_parent_id(db, parent_id)
        if parent_id == loc.id:
            raise LocationError("上级库位不能选择自己")

        cursor = db.get(Location, parent_id) if parent_id else None
        seen: set[int] = set()
        while cursor is not None and cursor.id not in seen:
            if cursor.id == loc.id:
                raise LocationError("不能把库位移动到自己的子库位下")
            seen.add(cursor.id)
            cursor = db.get(Location, cursor.parent_id) if cursor.parent_id else None

        duplicate = (
            db.query(Location)
            .filter(func.lower(Location.code) == code.lower(), Location.id != loc.id)
            .first()
        )
        if duplicate:
            raise LocationError(f"库位编码 {code} 已存在")

    loc.code = code
    loc.name = name
    loc.parent_id = parent_id
    loc.kind = kind
    loc.note = (note or "").strip() or None
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocationError("保存失败，请重试") from exc
    return loc


def get_temp_location(db) -> Location | None:
    """查找删除库位时使用的临时暂存库位。"""
    return _find_same_code(db, TEMP_LOCATION_CODE)


def ensure_temp_location(db) -> Location:
    """获取临时库位；不存在时创建。调用方负责提交事务。"""
    loc = get_temp_location(db)
    if loc is not None:
        return loc
    loc = Location(
        code=TEMP_LOCATION_CODE,
        name=TEMP_LOCATION_NAME,
        kind="cell",
        note="删除库位时自动暂存物料的临时库位",
        sort=0,
    )
    db.add(loc)
    db.flush()
    return loc


def _unique_ids(ids, label: str = "库位") -> list[int]:
    values: list[int] = []
    for raw in ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise LocationError(f"所选{label}无效") from exc
        if value <= 0:
            raise LocationError(f"所选{label}无效")
        if value not in values:
            values.append(value)
    if not values:
        raise LocationError(f"请先勾选要操作的{label}")
    return values


@serialized_write
def delete_locations(db, ids) -> LocationDeleteResult:
    """删除库位；有库存的批次自动移到临时库位。

    批量删除时，如果选中了父库位，必须同时选中其全部子库位。
    所有校验在写入前完成，避免批量删除只成功一部分。
    """
    location_ids = _unique_ids(ids)
    locations = db.query(Location).filter(Location.id.in_(location_ids)).all()
    by_id = {loc.id: loc for loc in locations}
    missing = [str(loc_id) for loc_id in location_ids if loc_id not in by_id]
    if missing:
        raise LocationError(f"部分库位不存在，请刷新后重试（ID: {'、'.join(missing)}）")

    selected = set(location_ids)
    errors: list[str] = []
    for loc in locations:
        if loc.code and loc.code.casefold() == TEMP_LOCATION_CODE.casefold():
            errors.append(f"{loc.code} 是临时库位，不能删除")
        children = db.query(Location).filter(Location.parent_id == loc.id).all()
        unselected = [child.code for child in children if child.id not in selected]
        if unselected:
            shown = "、".join(unselected[:4])
            if len(unselected) > 4:
                shown += " 等"
            errors.append(f"{loc.code} 还有未选中的子库位（{shown}）")
        if db.query(Loan).filter(
            Loan.status == "open", Loan.out_location_id == loc.id,
        ).count():
            errors.append(f"{loc.code} 有未归还借出")
    if errors:
        shown = "；".join(errors[:6])
        if len(errors) > 6:
            shown += f"；等 {len(errors)} 项"
        raise LocationError(shown)

    lots = db.query(StockLot).filter(StockLot.location_id.in_(location_ids)).all()
    positive_lots = [lot for lot in lots if float(lot.qty or 0) > 0]

    temp = None
    deleted_codes = [loc.code for loc in locations]
    temp_code = ""
    moved_lot_count = len(positive_lots)
    moved_part_count = len({lot.part_id for lot in positive_lots})
    moved_qty = sum(float(lot.qty or 0) for lot in positive_lots)
    try:
        if positive_lots:
            temp = ensure_temp_location(db)
            temp_code = temp.code
            for lot in positive_lots:
                source_id = lot.location_id
                lot.location_id = temp.id
                db.add(StockMove(
                    part_id=lot.part_id,
                    lot_id=lot.id,
                    type="move",
                    qty=float(lot.qty or 0),
                    from_location_id=source_id,
                    to_location_id=temp.id,
                    unit_price=lot.unit_price,
                    ref_type="manual",
                    note="删除库位时自动暂存",
                ))
        # 空批次保留历史，但解除库位引用，避免删除库位后留下悬空位置。
        for lot in lots:
            if float(lot.qty or 0) <= 0:
                lot.location_id = None

        for part in db.query(Part).filter(Part.default_location_id.in_(location_ids)).all():
            part.default_location_id = None

        locations_by_id = {loc.id: loc for loc in locations}

        def depth(loc: Location) -> int:
            value = 0
            current = loc
            seen: set[int] = set()
            while current.parent_id in locations_by_id and current.id not in seen:
                seen.add(current.id)
                current = locations_by_id[current.parent_id]
                value += 1
            return value

        for loc in sorted(locations, key=depth, reverse=True):
            db.delete(loc)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocationError("删除库位失败，请重试") from exc

    return LocationDeleteResult(
        deleted_codes=deleted_codes,
        moved_lot_count=moved_lot_count,
        moved_part_count=moved_part_count,
        moved_qty=moved_qty,
        temp_code=temp_code,
    )


@serialized_write
def move_parts_from_location(db, from_location_id, to_location_id, part_ids) -> LocationMoveResult:
    """把所选物料在源库位的全部在库批次移到目标库位。"""
    source = db.get(Location, from_location_id)
    target = db.get(Location, to_location_id)
    if source is None:
        raise LocationError("源库位不存在")
    if target is None:
        raise LocationError("目标库位不存在")
    if source.id == target.id:
        raise LocationError("目标库位不能与当前库位相同")

    selected_ids = _unique_ids(part_ids, label="物料")
    found_parts = db.query(Part).filter(Part.id.in_(selected_ids)).all()
    if len(found_parts) != len(selected_ids):
        raise LocationError("部分物料不存在，请刷新后重试")

    lots = (
        db.query(StockLot)
        .filter(
            StockLot.location_id == source.id,
            StockLot.part_id.in_(selected_ids),
            StockLot.qty > 0,
        )
        .all()
    )
    if not lots:
        raise LocationError("所选物料在当前库位没有库存")

    moved_qty = 0.0
    try:
        for lot in lots:
            qty = float(lot.qty or 0)
            moved_qty += qty
            lot.location_id = target.id
            db.add(StockMove(
                part_id=lot.part_id,
                lot_id=lot.id,
                type="move",
                qty=qty,
                from_location_id=source.id,
                to_location_id=target.id,
                unit_price=lot.unit_price,
                ref_type="manual",
                note="库位内批量移库",
            ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocationError("移库失败，请重试") from exc

    return LocationMoveResult(
        part_count=len({lot.part_id for lot in lots}),
        lot_count=len(lots),
        qty=moved_qty,
        to_code=target.code,
    )
