import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import func

from ..models import Category, Location, Part, PartSource, Supplier
from .stock import next_part_code, stock_in

FIELD_ALIASES = {
    "supplier_part_no": ["料号", "立创料号", "供应商料号", "商品编号", "器件编号", "supplier part", "supplierpart", "part no", "partno", "编号"],
    "model": ["型号", "规格型号", "商品型号", "产品型号", "规格", "model", "comment", "value", "part number"],
    "name": ["商品名称", "名称", "物料名称", "品名", "描述", "description", "product name"],
    "brand": ["品牌", "厂家", "制造商", "brand", "manufacturer", "mfr"],
    "package": ["封装", "封装规格", "footprint", "package", "case"],
    "qty": ["数量", "库存", "采购数量", "quantity", "qty", "amount"],
    "price": ["单价", "价格", "含税单价", "price", "unit price"],
    "lot_no": ["批次", "批号", "lot", "lot no"],
    "date_code": ["日期码", "生产日期", "date code", "dc", "date"],
    "location_code": ["库位", "库位编码", "location", "location code"],
    "designators": ["位号", "designator", "designators", "reference", "ref"],
}

CATEGORY_KEYWORDS = [
    ("电阻", ["电阻", "resistor", "ohm", "Ω"]),
    ("电容", ["电容", "capacitor", "farad", "uf", "nf", "pf"]),
    ("电感", ["电感", "inductor", "henry", "uh", "mh"]),
    ("二极管", ["二极管", "diode", "led", "ss14", "1n4"]),
    ("三极管/MOS", ["三极管", "mos", "mosfet", "bjt", "npn", "pnp"]),
    ("集成电路", ["芯片", "ic", "mcu", "stm32", "esp32", "op amp", "稳压", "ldo"]),
    ("连接器", ["连接器", "端子", "排针", "排母", "header", "connector", "usb", "type-c"]),
    ("晶振/振荡器", ["晶振", "振荡", "crystal", "oscillator"]),
    ("传感器", ["传感器", "sensor"]),
    ("模块/开发板", ["模块", "开发板", "module", "board", "arduino", "raspberry"]),
    ("紧固件", ["螺丝", "螺母", "铜柱", "螺栓", "screw", "nut", "standoff"]),
    ("工具/耗材", ["工具", "焊锡", "胶带", "tool", "solder"]),
]


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip().lower()


def read_table(path: str | Path) -> tuple[list[str], list[list]]:
    p = Path(path)
    if p.suffix.lower() in (".csv", ".txt"):
        raw = p.read_bytes()
        text = None
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader]
    else:
        wb = load_workbook(p, read_only=True, data_only=True)
        ws = wb.active
        rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
    def _cell(v):
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, date):
            return v.strftime("%Y-%m-%d")
        return v

    rows = [[_cell(v) for v in row] for row in rows]
    rows = [r for r in rows if any(str(v).strip() for v in r)]
    if not rows:
        return [], []
    headers = [str(v).strip() for v in rows[0]]
    data = []
    for r in rows[1:]:
        r = list(r) + [""] * (len(headers) - len(r))
        data.append(r[: len(headers)])
    return headers, data


def auto_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for field, aliases in FIELD_ALIASES.items():
        best = -1
        for i, h in enumerate(headers):
            if i in used:
                continue
            hn = _norm(h)
            if not hn:
                continue
            for a in aliases:
                an = _norm(a)
                if hn == an:
                    best = i
                    break
            if best < 0:
                for a in aliases:
                    an = _norm(a)
                    if an and an in hn:
                        best = i
                        break
            if best >= 0:
                break
        if best >= 0:
            mapping[field] = best
            used.add(best)
    # 型号缺失时，用“名称”兜底
    if "model" not in mapping and "name" in mapping:
        mapping["model"] = mapping["name"]
    return mapping


def guess_category(db, text: str) -> int | None:
    low = (text or "").lower()
    for name, words in CATEGORY_KEYWORDS:
        if any(w.lower() in low for w in words):
            cat = db.query(Category).filter(Category.name == name).first()
            if cat:
                return cat.id
    return None


def _to_float(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except Exception:
        return None


def commit_rows(db, rows, mapping, location_id: int, supplier_id: int | None = None):
    stats = {"created": 0, "matched": 0, "stocked": 0, "skipped": 0, "errors": [], "warnings": []}
    default_loc = db.get(Location, location_id)
    for row_idx, row in enumerate(rows, start=2):
        try:
            def get(field):
                idx = mapping.get(field)
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx]).strip() if row[idx] is not None else ""

            supplier_no = get("supplier_part_no")
            model = get("model") or get("name")
            name = get("name") or model
            brand = get("brand")
            package = get("package")
            qty = _to_float(get("qty")) or 0
            price = _to_float(get("price"))
            lot_no = get("lot_no")
            date_code = get("date_code")
            loc_code = get("location_code")

            if not model and not supplier_no:
                stats["skipped"] += 1
                continue

            part = None
            if supplier_no:
                part = (
                    db.query(Part)
                    .filter(func.lower(Part.supplier_part_no) == supplier_no.lower())
                    .first()
                )
                if part is None:
                    source = (
                        db.query(PartSource)
                        .filter(func.lower(PartSource.supplier_part_no) == supplier_no.lower())
                        .first()
                    )
                    if source:
                        part = db.get(Part, source.part_id)
            if part is None and model:
                part = db.query(Part).filter(Part.model == model).first()
            if part is None and model:
                part = db.query(Part).filter(Part.model.ilike(model)).first()

            if part is None:
                part = Part(
                    code=next_part_code(db),
                    model=(model or supplier_no)[:200],
                    name=(name or model or supplier_no)[:300],
                    brand=brand[:120],
                    package=package[:80],
                    supplier_part_no=supplier_no[:120],
                    supplier_id=supplier_id,
                    category_id=guess_category(db, f"{model} {name}"),
                    default_location_id=location_id,
                    unit="PCS",
                    min_stock=0,
                )
                db.add(part)
                db.flush()
                stats["created"] += 1
            else:
                stats["matched"] += 1
                changed = False
                if brand and not part.brand:
                    part.brand = brand[:120]
                    changed = True
                if package and not part.package:
                    part.package = package[:80]
                    changed = True
                if supplier_no and not part.supplier_part_no:
                    part.supplier_part_no = supplier_no[:120]
                    changed = True
                if location_id and not part.default_location_id:
                    part.default_location_id = location_id
                    changed = True
                if changed:
                    db.flush()

            target_loc = location_id
            if loc_code:
                loc = (
                    db.query(Location)
                    .filter(func.lower(Location.code) == loc_code.lower())
                    .first()
                )
                if loc:
                    target_loc = loc.id
                else:
                    stats["warnings"].append(
                        f"第 {row_idx} 行：库位编码 {loc_code} 不存在，已放入所选库位"
                    )

            if qty > 0:
                stock_in(
                    db,
                    part.id,
                    target_loc,
                    qty,
                    lot_no=lot_no,
                    date_code=date_code,
                    unit_price=price,
                    supplier_id=supplier_id,
                    note="Excel 导入",
                    ref_type="import",
                )
                stats["stocked"] += 1
            db.commit()
        except Exception as exc:  # 单行失败不影响其它行
            db.rollback()
            stats["errors"].append(f"第 {row_idx} 行：{exc}")
    return stats
