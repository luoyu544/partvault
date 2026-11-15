import base64
import io
import json
import re
import shutil
import socket
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import (
    BACKUP_DIR,
    BASE_DIR,
    DATA_DIR,
    EXPORT_DIR,
    UPLOAD_DIR,
    get_db,
    get_setting,
    init_db,
    set_setting,
    SessionLocal,
)
from .models import (
    Attachment,
    BomItem,
    BomProject,
    Category,
    Loan,
    Location,
    OcrJob,
    Part,
    PartSource,
    Setting,
    StockLot,
    StockMove,
    Supplier,
)
from .services import backup as backup_service
from .services import bom as bom_service
from .services import importer, labels, lcsc, ocr, printer as printer_service, repair, suppliers as supplier_service
from .services import loans as loan_service
from .services import locations as location_service
from .services.stock import (
    apply_stocktake,
    location_path,
    next_part_code,
    part_total_qty,
    serialized_write,
    stock_in,
    stock_out,
)

init_db()

app = FastAPI(title="元件库管家")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _icon(name: str, cls: str = "") -> Markup:
    """模板里统一使用的本地 SVG 图标，不依赖外网图标库。"""
    classes = f"icon {cls}".strip()
    return Markup(
        f'<svg class="{classes}" aria-hidden="true" focusable="false">'
        f'<use href="/static/icons.svg#i-{name}"></use></svg>'
    )


templates.env.globals["icon"] = _icon
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


def _int(v, default=None):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def _float(v, default=None):
    try:
        s = re.sub(r"[^\d.\-]", "", str(v))
        return float(s) if s not in ("", "-", ".") else default
    except Exception:
        return default


def _redirect(url: str, msg: str = "", err: str = ""):
    sep = "&" if "?" in url else "?"
    if msg:
        url = f"{url}{sep}msg={quote(msg)}"
    elif err:
        url = f"{url}{sep}err={quote(err)}"
    return RedirectResponse(url=url, status_code=303)


# 手机摄像头当扫码枪：手机拍照/取景 -> 服务端识别 -> 电脑扫码页轮询取走
_PHONE_SCAN_TTL = 24 * 3600
_PHONE_SCANS: dict[str, dict] = {}


def _prune_phone_scans():
    now = time.time()
    for token, sess in list(_PHONE_SCANS.items()):
        if now - float(sess.get("created", now)) > _PHONE_SCAN_TTL:
            _PHONE_SCANS.pop(token, None)


def _new_phone_scan_session() -> str:
    _prune_phone_scans()
    token = uuid.uuid4().hex[:16]
    _PHONE_SCANS[token] = {
        "created": time.time(),
        "last_seen": 0.0,
        "last_poll": 0.0,
        "events": [],
        "next_id": 1,
        "last_code": "",
        "last_code_at": 0.0,
    }
    return token


def _get_phone_scan_session(token: str):
    _prune_phone_scans()
    return _PHONE_SCANS.get((token or "").strip())


def _phone_desktop_online(sess: dict, within: float = 30.0) -> bool:
    return time.time() - float(sess.get("last_poll", 0)) < within


def _queue_phone_scan(token: str, code: str) -> dict | None:
    sess = _get_phone_scan_session(token)
    if sess is None:
        return None
    code = (code or "").strip()
    if not code:
        return None
    now = time.time()
    # 实时取景会反复识别到同一个码，短时间内去重
    if code == sess.get("last_code") and now - float(sess.get("last_code_at", 0)) < 3.0:
        return None
    event = {
        "id": int(sess["next_id"]),
        "code": code,
        "at": datetime.now().strftime("%H:%M:%S"),
    }
    sess["next_id"] = int(sess["next_id"]) + 1
    sess["events"].append(event)
    sess["events"] = sess["events"][-50:]
    sess["last_code"] = code
    sess["last_code_at"] = now
    sess["last_seen"] = now
    return event


def _phone_code(codes: list[str]) -> str:
    for code in codes:
        if str(code).upper().startswith(("PV-P-", "PV-L-", "PV-B-")):
            return code
    # 立创包装袋常同时有二维码和 CODE128；优先取能识别出 C 码的那个。
    for code in codes:
        if lcsc.extract_code(code):
            return code
    return codes[0] if codes else ""


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def _phone_base_url(request: Request) -> str:
    hostname = request.url.hostname or "127.0.0.1"
    port = request.url.port or 8765
    if hostname in ("127.0.0.1", "localhost", "::1"):
        hostname = _lan_ip()
    return f"{request.url.scheme}://{hostname}:{port}"


def _qr_data_url(data: str) -> str:
    buf = io.BytesIO()
    labels.qr_image(data, box_size=7).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _qty_map(db: Session) -> dict[int, float]:
    rows = db.query(StockLot.part_id, func.sum(StockLot.qty)).group_by(StockLot.part_id).all()
    return {int(pid): float(q or 0) for pid, q in rows}


def _loc_map(db: Session) -> dict[int, str]:
    locs = db.query(Location).all()
    return {l.id: location_path(db, l) for l in locs}


def _price_map(db: Session) -> dict[int, float]:
    rows = (
        db.query(StockLot.part_id, func.max(StockLot.unit_price))
        .filter(StockLot.unit_price.isnot(None))
        .group_by(StockLot.part_id)
        .all()
    )
    return {int(pid): float(p) for pid, p in rows if p is not None}


def _part_locations_map(db: Session) -> dict[int, list[tuple[float, str]]]:
    rows = (
        db.query(StockLot.part_id, StockLot.location_id, func.sum(StockLot.qty))
        .filter(StockLot.qty > 0)
        .group_by(StockLot.part_id, StockLot.location_id)
        .all()
    )
    lm = _loc_map(db)
    out: dict[int, list[tuple[float, str]]] = {}
    for pid, lid, qty in rows:
        out.setdefault(int(pid), []).append((float(qty or 0), lm.get(lid, "未分配")))
    for entries in out.values():
        entries.sort(key=lambda x: (-x[0], x[1]))
    return out


def _open_loan_map(db: Session) -> dict[int, float]:
    rows = (
        db.query(Loan.part_id, func.sum(Loan.qty - Loan.returned_qty))
        .filter(Loan.status == "open", Loan.returned_qty < Loan.qty)
        .group_by(Loan.part_id)
        .all()
    )
    return {int(pid): float(q or 0) for pid, q in rows}


def _attach_part_meta(db: Session, parts):
    qm = _qty_map(db)
    lm = _loc_map(db)
    pm = _price_map(db)
    locations = _part_locations_map(db)
    open_loans = _open_loan_map(db)
    for p in parts:
        p._qty = qm.get(p.id, 0)
        p._price = pm.get(p.id)
        p._low = bool(p.min_stock and p._qty < p.min_stock)
        p._open_loan_qty = open_loans.get(p.id, 0)
        p._default_loc = lm.get(p.default_location_id, "")
        p._locations = locations.get(p.id, [])
        if p._locations:
            p._loc = p._locations[0][1]
            p._loc_full = "、".join(x[1] for x in p._locations)
            p._loc_summary = p._loc if len(p._locations) == 1 else f"{p._loc} 等 {len(p._locations)} 处"
        else:
            p._loc = lm.get(p.default_location_id, "")
            p._loc_full = p._loc
            p._loc_summary = (p._loc + "（常用）") if p._loc else ""
    return parts


def _static_version() -> str:
    """静态资源版本号，避免浏览器继续使用旧脚本。"""
    static_dir = BASE_DIR / "app" / "static"
    try:
        latest = max(
            item.stat().st_mtime for item in static_dir.iterdir() if item.is_file()
        )
        return str(int(latest))
    except (OSError, ValueError):
        return "1"


def _ctx(request: Request, db: Session, **kw):
    base = {
        "request": request,
        "site_name": get_setting(db, "site_name", "元件库管家"),
        "label_brand": get_setting(db, "label_brand", "元件库"),
        "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
        "open_loan_count": loan_service.open_loan_count(db),
        "static_version": _static_version(),
    }
    base.update(kw)
    return base


def _location_options(db: Session, exclude_ids=None):
    excluded = set(exclude_ids or ())
    locs = db.query(Location).order_by(Location.sort.asc(), Location.code.asc()).all()
    children = defaultdict(list)
    for l in locs:
        children[l.parent_id].append(l)
    out = []

    def walk(parent_id, depth):
        for l in children.get(parent_id, []):
            if l.id in excluded:
                continue
            out.append({"id": l.id, "name": l.name, "code": l.code, "depth": depth})
            walk(l.id, depth + 1)

    walk(None, 0)
    for l in locs:
        if l.id in excluded:
            continue
        if all(o["id"] != l.id for o in out) and l.parent_id:
            out.append({"id": l.id, "name": l.name, "code": l.code, "depth": 1})
    return out


def _location_descendant_ids(db: Session, location_id: int) -> set[int]:
    """返回某个库位的全部下级库位 ID，用于编辑时排除非法上级。"""
    children = defaultdict(list)
    for l in db.query(Location).all():
        children[l.parent_id].append(l.id)
    found: set[int] = set()
    stack = [location_id]
    while stack:
        parent = stack.pop()
        for child in children.get(parent, ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _categories(db: Session):
    return db.query(Category).order_by(Category.sort.asc(), Category.id.asc()).all()


def _suppliers(db: Session):
    return db.query(Supplier).order_by(Supplier.id.asc()).all()


def _apply_lcsc_code(db: Session, part: Part, code: str, mode: str = "blank") -> str:
    """应用用户选中的立创 C 码；失败时返回给用户看的提示，不阻断入库。"""
    code = (code or "").strip().upper()
    if not code:
        return ""
    try:
        candidate = lcsc.lookup(code)
        lcsc.apply_candidate(db, part, candidate, mode="overwrite" if mode == "overwrite" else "blank")
        # 表单里的 C 码可能是前端自动填的；主供应商不是立创时，只保留 PartSource。
        supplier = lcsc.ensure_lcsc_supplier(db)
        if part.supplier_id != supplier.id and (part.supplier_part_no or "").strip().upper() == code:
            part.supplier_part_no = ""
        return ""
    except lcsc.LcscError as exc:
        return f"立创资料未应用：{exc}"


def _lookup_location(db: Session, code: str):
    return db.query(Location).filter(func.lower(Location.code) == (code or "").strip().lower()).first()


def _find_part(db: Session, code: str):
    c = (code or "").strip()
    if not c:
        return None
    low = c.lower()
    if c.upper().startswith("PV-P-"):
        p = db.query(Part).filter(func.lower(Part.code) == low).first()
        if p:
            return p
    p = db.query(Part).filter(or_(
        func.lower(Part.code) == low,
        func.lower(Part.supplier_part_no) == low,
    )).first()
    if p is None:
        source = db.query(PartSource).filter(func.lower(PartSource.supplier_part_no) == low).first()
        if source:
            p = db.get(Part, source.part_id)
    if p is None:
        p = db.query(Part).filter(Part.model.ilike(c)).first()
    return p


def _part_scan_payload(db: Session, p: Part) -> dict:
    locs = {l.id: l for l in db.query(Location).all()}
    rows = (
        db.query(StockLot.location_id, func.sum(StockLot.qty))
        .filter(StockLot.part_id == p.id, StockLot.qty > 0)
        .group_by(StockLot.location_id)
        .all()
    )
    locations = []
    for lid, qty in rows:
        loc = locs.get(lid)
        locations.append({
            "id": loc.id if loc else None,
            "code": loc.code if loc else "",
            "name": loc.name if loc else "未分配",
            "path": location_path(db, loc) if loc else "未分配",
            "qty": float(qty or 0),
        })
    locations.sort(key=lambda x: x["path"])
    open_rows = []
    for loan in loan_service.open_loans(db, part_id=p.id):
        out = locs.get(loan.out_location_id)
        open_rows.append({
            "id": loan.id,
            "qty": float(loan.qty or 0),
            "returned_qty": float(loan.returned_qty or 0),
            "outstanding": float(loan.outstanding_qty),
            "borrower": loan.borrower or "",
            "out_location": location_path(db, out) if out else "",
            "created_at": loan.created_at.strftime("%Y-%m-%d %H:%M") if loan.created_at else "",
        })
    return {
        "kind": "part",
        "part": {
            "id": p.id,
            "code": p.code,
            "model": p.model,
            "name": p.name or "",
            "unit": p.unit or "PCS",
            "qty": part_total_qty(db, p.id),
            "locations": locations,
            "open_qty": sum(x["outstanding"] for x in open_rows),
            "loans": open_rows,
        },
    }


def _lcsc_part_scan_payload(candidate: dict) -> dict:
    """把立创查询结果包装成扫码页可渲染的“待建档物料”。"""
    code = (candidate.get("code") or "").upper()
    model = candidate.get("mpn") or candidate.get("name") or code
    return {
        "kind": "part",
        "part": {
            "id": None,
            "code": "",
            "model": model,
            "name": candidate.get("name") or "",
            "brand": candidate.get("brand") or "",
            "package": candidate.get("package") or "",
            "unit": "PCS",
            "qty": 0,
            "locations": [],
            "open_qty": 0,
            "loans": [],
            "lcsc_code": code,
            "lcsc_draft": True,
            "lcsc_price": candidate.get("price_min"),
            "lcsc_stock": candidate.get("stock"),
        },
    }


def _location_scan_payload(db: Session, loc: Location) -> dict:
    qty, part_count = (
        db.query(func.coalesce(func.sum(StockLot.qty), 0), func.count(func.distinct(StockLot.part_id)))
        .filter(StockLot.location_id == loc.id, StockLot.qty > 0)
        .first()
    )
    return {
        "kind": "location",
        "location": {
            "id": loc.id,
            "code": loc.code,
            "name": loc.name,
            "path": location_path(db, loc),
            "qty": float(qty or 0),
            "part_count": int(part_count or 0),
        },
    }


def _loan_rows(db: Session, loans):
    locs = {l.id: l for l in db.query(Location).all()}
    rows = []
    for loan in loans:
        out_loc = locs.get(loan.out_location_id)
        ret_loc = locs.get(loan.return_location_id)
        rows.append({
            "loan": loan,
            "part": db.get(Part, loan.part_id),
            "out_loc": out_loc,
            "return_loc": ret_loc,
            "out_path": location_path(db, out_loc) if out_loc else "",
            "return_path": location_path(db, ret_loc) if ret_loc else "",
        })
    return rows


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    parts = _attach_part_meta(db, db.query(Part).filter(Part.status == "active").all())
    total_qty = sum(p._qty for p in parts)
    low = [p for p in parts if p._low]
    low.sort(key=lambda p: (p._qty - p.min_stock))
    loc_count = db.query(Location).count()
    moves = db.query(StockMove).order_by(StockMove.created_at.desc(), StockMove.id.desc()).limit(10).all()
    move_rows = []
    for m in moves:
        p = db.get(Part, m.part_id)
        fl = db.get(Location, m.from_location_id) if m.from_location_id else None
        tl = db.get(Location, m.to_location_id) if m.to_location_id else None
        move_rows.append({"m": m, "part": p, "from": fl, "to": tl})
    open_loans = loan_service.open_loans(db)
    now = datetime.now()
    loan_rows = []
    for loan in open_loans:
        age_days = max((now - loan.created_at).days, 0) if loan.created_at else 0
        loan_rows.append({
            "loan": loan,
            "part": db.get(Part, loan.part_id),
            "out_loc": db.get(Location, loan.out_location_id) if loan.out_location_id else None,
            "age_days": age_days,
        })
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request, db,
            active="dashboard",
            parts_count=len(parts),
            total_qty=total_qty,
            loc_count=loc_count,
            low_count=len(low),
            low=low[:8],
            moves=move_rows,
            open_loans=loan_rows[:8],
            open_loan_count=len(loan_rows),
            open_loan_qty=loan_service.open_loan_qty(db),
        ),
    )


@app.get("/parts", response_class=HTMLResponse)
def parts_list(
    request: Request,
    q: str = "",
    category: str = "",
    location: str = "",
    low: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    query = db.query(Part)
    if q.strip():
        like = f"%{q.strip()}%"
        source_ids = select(PartSource.part_id).where(PartSource.supplier_part_no.ilike(like))
        query = query.filter(or_(
            Part.model.ilike(like), Part.name.ilike(like), Part.code.ilike(like),
            Part.supplier_part_no.ilike(like), Part.brand.ilike(like), Part.note.ilike(like),
            Part.id.in_(source_ids),
        ))
    cid = _int(category)
    if cid:
        query = query.filter(Part.category_id == cid)
    lid = _int(location)
    if lid:
        location_ids = {lid} | _location_descendant_ids(db, lid)
        sub = db.query(StockLot.part_id).filter(StockLot.location_id.in_(location_ids)).subquery()
        query = query.filter(or_(
            Part.default_location_id.in_(location_ids),
            Part.id.in_(sub),
        ))
    parts = _attach_part_meta(db, query.order_by(Part.updated_at.desc(), Part.id.desc()).all())
    if low:
        parts = [p for p in parts if p._low]
    total = len(parts)
    per = 50
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    parts = parts[(page - 1) * per: page * per]
    cat_map = {c.id: c.name for c in _categories(db)}

    def page_url(target: int) -> str:
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
        params.append(("page", str(target)))
        return "/parts?" + urlencode(params)

    return templates.TemplateResponse(
        request,
        "parts.html",
        _ctx(
            request, db, active="parts", parts=parts, total=total, page=page, pages=pages,
            q=q, category=category, location=location, low=low, cat_map=cat_map,
            categories=_categories(db), locations=_location_options(db), page_url=page_url,
        ),
    )


def _save_upload(file: UploadFile | None, prefix: str = "img") -> str:
    if not file or not file.filename:
        return ""
    ext = Path(file.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".xlsx", ".xls", ".csv"):
        ext = ".png"
    name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{ext}"
    dest = UPLOAD_DIR / name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return name


@app.get("/parts/new", response_class=HTMLResponse)
def part_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "part_form.html",
        _ctx(request, db, active="parts", part=None, categories=_categories(db),
             suppliers=_suppliers(db), locations=_location_options(db),
             lcsc_query="", lcsc_package="", lcsc_brand=""),
    )


@app.post("/parts/new")
def part_create(
    request: Request,
    model: str = Form(""),
    name: str = Form(""),
    brand: str = Form(""),
    package: str = Form(""),
    category_id: str = Form(""),
    default_location_id: str = Form(""),
    supplier_id: str = Form(""),
    supplier_part_no: str = Form(""),
    datasheet_url: str = Form(""),
    purchase_url: str = Form(""),
    min_stock: str = Form("0"),
    unit: str = Form("PCS"),
    attributes: str = Form(""),
    note: str = Form(""),
    initial_qty: str = Form(""),
    initial_lot: str = Form(""),
    initial_date_code: str = Form(""),
    initial_price: str = Form(""),
    lcsc_code: str = Form(""),
    lcsc_mode: str = Form("blank"),
    image: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    if not model.strip():
        return _redirect("/parts/new", err="型号不能为空")
    lid = _int(default_location_id)
    if lid and not db.get(Location, lid):
        return _redirect("/parts/new", err="常用库位不存在，请重新选择")
    p = Part(
        code=next_part_code(db),
        model=model.strip()[:200],
        name=name.strip()[:300] or model.strip()[:300],
        brand=brand.strip()[:120],
        package=package.strip()[:80],
        category_id=_int(category_id),
        default_location_id=lid,
        supplier_id=_int(supplier_id),
        supplier_part_no=supplier_part_no.strip()[:120],
        datasheet_url=datasheet_url.strip(),
        purchase_url=purchase_url.strip(),
        min_stock=max(_float(min_stock, 0) or 0, 0),
        unit=unit.strip() or "PCS",
        attributes=attributes.strip(),
        note=note.strip(),
    )
    img = _save_upload(image, "part")
    if img:
        p.image_path = img
    db.add(p)
    db.flush()
    lcsc_warn = _apply_lcsc_code(db, p, lcsc_code, lcsc_mode)
    qty = _float(initial_qty, 0) or 0
    if qty > 0:
        if not lid:
            db.rollback()
            return _redirect("/parts/new", err="填了初始数量就必须选库位")
        stock_in(db, p.id, lid, qty, lot_no=initial_lot.strip(),
                 date_code=initial_date_code.strip(), unit_price=_float(initial_price),
                 supplier_id=p.supplier_id, note="初始入库", ref_type="manual")
    else:
        db.commit()
    msg = f"已创建 {p.model}" + (f"；{lcsc_warn}" if lcsc_warn else "")
    return _redirect(f"/parts/{p.id}", msg=msg)


@app.get("/parts/{part_id}", response_class=HTMLResponse)
def part_detail(part_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    _attach_part_meta(db, [p])
    lots = db.query(StockLot).filter(StockLot.part_id == p.id, StockLot.qty > 0).order_by(StockLot.received_at.asc()).all()
    locs = {l.id: l for l in db.query(Location).all()}
    lot_rows = [{"lot": l, "loc": locs.get(l.location_id), "path": location_path(db, locs.get(l.location_id))} for l in lots]
    moves = db.query(StockMove).filter(StockMove.part_id == p.id).order_by(StockMove.created_at.desc(), StockMove.id.desc()).limit(80).all()
    move_rows = []
    for m in moves:
        move_rows.append({
            "m": m,
            "from": locs.get(m.from_location_id),
            "to": locs.get(m.to_location_id),
        })
    sources = db.query(PartSource).filter(PartSource.part_id == p.id).all()
    attachments = db.query(Attachment).filter(Attachment.part_id == p.id).order_by(Attachment.created_at.desc()).all()
    cat = db.get(Category, p.category_id) if p.category_id else None
    sup = db.get(Supplier, p.supplier_id) if p.supplier_id else None
    part_loans = []
    for loan in loan_service.open_loans(db, part_id=p.id):
        part_loans.append({
            "loan": loan,
            "out_loc": locs.get(loan.out_location_id),
        })
    return templates.TemplateResponse(
        request,
        "part_detail.html",
        _ctx(request, db, active="parts", part=p, lots=lot_rows, moves=move_rows,
             sources=sources, attachments=attachments, category=cat, supplier=sup,
             part_loans=part_loans,
             supplier_map={s.id: s.name for s in _suppliers(db)},
             locations=_location_options(db), suppliers=_suppliers(db)),
    )


@app.get("/parts/{part_id}/edit", response_class=HTMLResponse)
def part_edit(part_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    return templates.TemplateResponse(
        request,
        "part_form.html",
        _ctx(request, db, active="parts", part=p, categories=_categories(db),
             suppliers=_suppliers(db), locations=_location_options(db),
             lcsc_query=lcsc.build_query(p.name or "", p.model or "", p.package or "", p.brand or ""),
             lcsc_package=p.package or "", lcsc_brand=p.brand or ""),
    )


@app.post("/parts/{part_id}/edit")
def part_update(
    part_id: int,
    model: str = Form(""),
    name: str = Form(""),
    brand: str = Form(""),
    package: str = Form(""),
    category_id: str = Form(""),
    default_location_id: str = Form(""),
    supplier_id: str = Form(""),
    supplier_part_no: str = Form(""),
    datasheet_url: str = Form(""),
    purchase_url: str = Form(""),
    min_stock: str = Form("0"),
    unit: str = Form("PCS"),
    attributes: str = Form(""),
    note: str = Form(""),
    status: str = Form("active"),
    lcsc_code: str = Form(""),
    lcsc_mode: str = Form("blank"),
    image: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    if not model.strip():
        return _redirect(f"/parts/{part_id}/edit", err="型号不能为空")
    dlid = _int(default_location_id)
    if dlid and not db.get(Location, dlid):
        return _redirect(f"/parts/{part_id}/edit", err="常用库位不存在，请重新选择")
    p.model = model.strip()[:200]
    p.name = name.strip()[:300] or p.model
    p.brand = brand.strip()[:120]
    p.package = package.strip()[:80]
    p.category_id = _int(category_id)
    p.default_location_id = dlid
    p.supplier_id = _int(supplier_id)
    p.supplier_part_no = supplier_part_no.strip()[:120]
    p.datasheet_url = datasheet_url.strip()
    p.purchase_url = purchase_url.strip()
    p.min_stock = max(_float(min_stock, 0) or 0, 0)
    p.unit = unit.strip() or "PCS"
    p.attributes = attributes.strip()
    p.note = note.strip()
    p.status = status if status in ("active", "archived") else "active"
    img = _save_upload(image, "part")
    if img:
        p.image_path = img
    lcsc_warn = _apply_lcsc_code(db, p, lcsc_code, lcsc_mode)
    db.commit()
    msg = "已保存" + (f"；{lcsc_warn}" if lcsc_warn else "")
    return _redirect(f"/parts/{part_id}", msg=msg)


@app.post("/parts/{part_id}/lcsc")
def part_apply_lcsc(
    part_id: int,
    code: str = Form(""),
    mode: str = Form("blank"),
    db: Session = Depends(get_db),
):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    code = (code or "").strip().upper()
    if not code:
        return _redirect(f"/parts/{part_id}", err="请先选择一个立创器件")
    try:
        candidate = lcsc.lookup(code)
        result = lcsc.apply_candidate(db, p, candidate, mode="overwrite" if mode == "overwrite" else "blank")
        db.commit()
    except lcsc.LcscError as exc:
        db.rollback()
        return _redirect(f"/parts/{part_id}", err=f"立创资料应用失败：{exc}")
    changed = result.get("changed") or []
    if mode == "overwrite":
        msg = f"已按立创 {code} 更新" + (f"：{'、'.join(changed)}" if changed else "（没有可更新字段）")
    else:
        msg = f"已补齐立创 {code} 资料" + (f"：{'、'.join(changed)}" if changed else "（已有字段保持不变）")
    return _redirect(f"/parts/{part_id}", msg=msg)


@app.post("/parts/{part_id}/delete")
@serialized_write
def part_delete(part_id: int, db: Session = Depends(get_db)):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    if db.query(Loan).filter(
        Loan.part_id == part_id, Loan.status == "open", Loan.returned_qty < Loan.qty,
    ).count():
        return _redirect(f"/parts/{part_id}", err="该物料还有未归还借出，请先归还后再删除")
    db.query(BomItem).filter(BomItem.part_id == part_id).update(
        {BomItem.part_id: None, BomItem.matched: False}, synchronize_session=False,
    )
    db.query(StockMove).filter(StockMove.part_id == part_id).delete()
    db.query(Loan).filter(Loan.part_id == part_id).delete()
    db.query(StockLot).filter(StockLot.part_id == part_id).delete()
    db.query(PartSource).filter(PartSource.part_id == part_id).delete()
    db.query(Attachment).filter(Attachment.part_id == part_id).delete()
    db.query(OcrJob).filter(OcrJob.part_id == part_id).delete()
    db.delete(p)
    db.commit()
    return _redirect("/parts", msg="已删除")


@app.post("/parts/{part_id}/stock")
def part_stock(
    part_id: int,
    action: str = Form("in"),
    qty: str = Form(""),
    location_id: str = Form(""),
    lot_no: str = Form(""),
    date_code: str = Form(""),
    price: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.get(Part, part_id)
    if not p:
        return _redirect("/parts", err="物料不存在")
    q = _float(qty)
    if not q or q <= 0:
        return _redirect(f"/parts/{part_id}", err="数量无效")
    try:
        if action == "out":
            stock_out(db, part_id, q, location_id=_int(location_id), note=note)
            return _redirect(f"/parts/{part_id}", msg=f"已出库 {q:g}")
        stock_in(db, part_id, _int(location_id) or p.default_location_id, q,
                 lot_no=lot_no, date_code=date_code, unit_price=_float(price), note=note)
        return _redirect(f"/parts/{part_id}", msg=f"已入库 {q:g}")
    except Exception as exc:
        return _redirect(f"/parts/{part_id}", err=str(exc))


@app.post("/parts/{part_id}/source")
def add_source(
    part_id: int,
    supplier_id: str = Form(""),
    supplier_part_no: str = Form(""),
    url: str = Form(""),
    price: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    if not db.get(Part, part_id):
        return _redirect("/parts", err="物料不存在")
    db.add(PartSource(part_id=part_id, supplier_id=_int(supplier_id),
                      supplier_part_no=supplier_part_no.strip()[:120],
                      url=url.strip(), price=_float(price), note=note.strip()))
    db.commit()
    return _redirect(f"/parts/{part_id}", msg="已添加来源")


def _locations_context(request: Request, db: Session, **extra):
    locs = db.query(Location).order_by(Location.sort.asc(), Location.code.asc()).all()
    children = defaultdict(list)
    for l in locs:
        children[l.parent_id].append(l)

    # 库位树显示“含子库位”的合计：父库位也能看到整柜/整层的库存。
    stock_rows = (
        db.query(StockLot.location_id, StockLot.part_id, func.sum(StockLot.qty))
        .filter(StockLot.qty > 0)
        .group_by(StockLot.location_id, StockLot.part_id)
        .all()
    )
    direct_parts: dict[int, set[int]] = defaultdict(set)
    direct_qty: dict[int, float] = defaultdict(float)
    for location_id, part_id, stock_qty in stock_rows:
        if location_id is None:
            continue
        direct_parts[location_id].add(part_id)
        direct_qty[location_id] += float(stock_qty or 0)

    memo_parts: dict[int, set[int]] = {}
    memo_qty: dict[int, float] = {}
    visiting: set[int] = set()

    def subtree_totals(location_id: int):
        if location_id in memo_parts:
            return memo_parts[location_id], memo_qty[location_id]
        if location_id in visiting:
            return set(), 0.0
        visiting.add(location_id)
        parts_for_node = set(direct_parts.get(location_id, ()))
        qty_for_node = float(direct_qty.get(location_id, 0))
        for child in children.get(location_id, []):
            child_parts, child_qty = subtree_totals(child.id)
            parts_for_node |= child_parts
            qty_for_node += child_qty
        visiting.discard(location_id)
        memo_parts[location_id] = parts_for_node
        memo_qty[location_id] = qty_for_node
        return parts_for_node, qty_for_node

    counts = {}
    qty = {}
    for loc in locs:
        parts_for_node, qty_for_node = subtree_totals(loc.id)
        counts[loc.id] = len(parts_for_node)
        qty[loc.id] = qty_for_node

    ctx = _ctx(
        request, db, active="locations", locs=locs, children=children,
        counts=counts, qty=qty, locations=_location_options(db),
        temp_location_code=location_service.TEMP_LOCATION_CODE,
        open_tab="single",
        single_code="", single_name="", single_parent_id="",
        single_kind="cell", single_note="",
        batch_codes="", batch_parent_id="", batch_kind="cell",
    )
    ctx.update(extra)
    return ctx


def _render_locations(request: Request, db: Session, status_code: int = 200, **extra):
    return templates.TemplateResponse(
        request,
        "locations.html",
        _locations_context(request, db, **extra),
        status_code=status_code,
    )


@app.get("/locations", response_class=HTMLResponse)
def locations_page(request: Request, db: Session = Depends(get_db)):
    return _render_locations(request, db)


@app.post("/locations/new")
def location_create(
    request: Request,
    code: str = Form(""),
    name: str = Form(""),
    parent_id: str = Form(""),
    kind: str = Form("cell"),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        loc = location_service.create_location(
            db, code=code, name=name, parent_id=parent_id, kind=kind, note=note,
        )
    except location_service.LocationError as exc:
        return _render_locations(
            request, db, status_code=400, open_tab="single", err=str(exc),
            single_code=code, single_name=name, single_parent_id=parent_id,
            single_kind=kind, single_note=note,
        )
    return _redirect("/locations", msg=f"已添加库位 {loc.code}")


@app.post("/locations/batch")
def location_batch_create(
    request: Request,
    codes: str = Form(""),
    parent_id: str = Form(""),
    kind: str = Form("cell"),
    db: Session = Depends(get_db),
):
    try:
        created = location_service.create_locations_batch(
            db, text=codes, parent_id=parent_id, kind=kind,
        )
    except location_service.LocationError as exc:
        return _render_locations(
            request, db, status_code=400, open_tab="batch", err=str(exc),
            batch_codes=codes, batch_parent_id=parent_id, batch_kind=kind,
        )
    preview = "、".join(loc.code for loc in created[:4])
    if len(created) > 4:
        preview += " 等"
    return _redirect("/locations", msg=f"已批量新建 {len(created)} 个库位（{preview}）")


def _location_delete_message(result) -> str:
    if len(result.deleted_codes) == 1:
        msg = f"已删除库位 {result.deleted_codes[0]}"
    else:
        preview = "、".join(result.deleted_codes[:4])
        if len(result.deleted_codes) > 4:
            preview += " 等"
        msg = f"已删除 {len(result.deleted_codes)} 个库位（{preview}）"
    if result.moved_lot_count:
        msg += (
            f"；{result.moved_lot_count} 个批次（{result.moved_part_count} 种物料，"
            f"共 {result.moved_qty:g} 件）已移到 {result.temp_code} 临时库位"
        )
    return msg


def _location_move_message(result) -> str:
    return (
        f"已将 {result.part_count} 种物料（{result.lot_count} 个批次，"
        f"共 {result.qty:g} 件）移到 {result.to_code}"
    )


@app.post("/locations/batch-delete")
def location_batch_delete(
    ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    try:
        result = location_service.delete_locations(db, ids)
    except location_service.LocationError as exc:
        return _redirect("/locations", err=str(exc))
    return _redirect("/locations", msg=_location_delete_message(result))


@app.post("/locations/{loc_id}/delete")
def location_delete(loc_id: int, db: Session = Depends(get_db)):
    try:
        result = location_service.delete_locations(db, [loc_id])
    except location_service.LocationError as exc:
        return _redirect("/locations", err=str(exc))
    return _redirect("/locations", msg=_location_delete_message(result))


@app.post("/locations/{loc_id}/move")
def location_move_parts(
    loc_id: int,
    to_location_id: str = Form(""),
    part_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    try:
        target_id = _int(to_location_id)
        if target_id is None:
            raise location_service.LocationError("请选择目标库位")
        result = location_service.move_parts_from_location(
            db, from_location_id=loc_id, to_location_id=target_id, part_ids=part_ids,
        )
    except location_service.LocationError as exc:
        return _redirect(f"/locations/{loc_id}", err=str(exc))
    return _redirect(f"/locations/{loc_id}", msg=_location_move_message(result))


def _render_location_edit(
    request: Request,
    db: Session,
    loc: Location,
    status_code: int = 200,
    error: str = "",
    **form_values,
):
    def pick(key, fallback):
        return form_values[key] if key in form_values else fallback

    is_temp = (loc.code or "").casefold() == location_service.TEMP_LOCATION_CODE.casefold()
    form = {
        "code": str(pick("code", loc.code) or ""),
        "name": str(pick("name", loc.name or "") or ""),
        "parent_id": str(pick("parent_id", loc.parent_id if loc.parent_id is not None else "") or ""),
        "kind": str(pick("kind", loc.kind) or "cell"),
        "note": str(pick("note", loc.note or "") or ""),
    }
    if is_temp:
        form["code"] = loc.code
        form["parent_id"] = str(loc.parent_id or "")

    exclude_ids = {loc.id} | _location_descendant_ids(db, loc.id)
    return templates.TemplateResponse(
        request,
        "location_form.html",
        _ctx(
            request, db, active="locations", loc=loc, form=form,
            locations=_location_options(db, exclude_ids=exclude_ids),
            is_temp=is_temp, temp_location_code=location_service.TEMP_LOCATION_CODE,
            err=error,
        ),
        status_code=status_code,
    )


@app.get("/locations/{loc_id}/edit", response_class=HTMLResponse)
def location_edit(loc_id: int, request: Request, db: Session = Depends(get_db)):
    loc = db.get(Location, loc_id)
    if not loc:
        return _redirect("/locations", err="库位不存在")
    return _render_location_edit(request, db, loc)


@app.post("/locations/{loc_id}/edit")
def location_update(
    loc_id: int,
    request: Request,
    code: str = Form(""),
    name: str = Form(""),
    parent_id: str = Form(""),
    kind: str = Form("cell"),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    loc = db.get(Location, loc_id)
    if not loc:
        return _redirect("/locations", err="库位不存在")
    try:
        updated = location_service.update_location(
            db, loc.id, code=code, name=name, parent_id=parent_id, kind=kind, note=note,
        )
    except location_service.LocationError as exc:
        return _render_location_edit(
            request, db, loc, status_code=400, error=str(exc),
            code=code, name=name, parent_id=parent_id, kind=kind, note=note,
        )
    return _redirect(f"/locations/{updated.id}", msg=f"已保存库位 {updated.code}")


@app.get("/locations/{loc_id}", response_class=HTMLResponse)
def location_detail(loc_id: int, request: Request, db: Session = Depends(get_db)):
    loc = db.get(Location, loc_id)
    if not loc:
        return _redirect("/locations", err="库位不存在")
    rows = (
        db.query(Part, StockLot)
        .join(StockLot, StockLot.part_id == Part.id)
        .filter(StockLot.location_id == loc_id, StockLot.qty > 0)
        .order_by(Part.model.asc())
        .all()
    )
    merged = {}
    for p, lot in rows:
        if p.id not in merged:
            p._qty = 0
            merged[p.id] = p
        merged[p.id]._qty += float(lot.qty or 0)
    parts = list(merged.values())
    path = location_path(db, loc)
    return templates.TemplateResponse(
        request,
        "location_detail.html",
        _ctx(request, db, active="locations", loc=loc, parts=parts, path=path,
             locations=_location_options(db),
             temp_location_code=location_service.TEMP_LOCATION_CODE),
    )


@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    moves_query = db.query(StockMove).order_by(StockMove.created_at.desc(), StockMove.id.desc())
    parts_all = _attach_part_meta(db, db.query(Part).filter(Part.status == "active").order_by(Part.model.asc()).all())
    if q.strip():
        like = f"%{q.strip()}%"
        part_ids = [p.id for p in parts_all if q.strip().lower() in (p.model or "").lower()
                    or q.strip().lower() in (p.name or "").lower()
                    or q.strip().lower() in (p.supplier_part_no or "").lower()
                    or q.strip().lower() in (p.code or "").lower()]
        moves_query = moves_query.filter(StockMove.part_id.in_(part_ids or [0]))
    moves = moves_query.limit(200).all()
    locs = {l.id: l for l in db.query(Location).all()}
    move_rows = [{"m": m, "part": db.get(Part, m.part_id),
                  "from": locs.get(m.from_location_id), "to": locs.get(m.to_location_id)} for m in moves]
    return templates.TemplateResponse(
        request,
        "stock.html",
        _ctx(request, db, active="stock", moves=move_rows, parts=parts_all, q=q,
             locations=_location_options(db)),
    )


@app.post("/stock/in")
def stock_in_page(
    part_id: str = Form(""),
    qty: str = Form(""),
    location_id: str = Form(""),
    lot_no: str = Form(""),
    date_code: str = Form(""),
    price: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    pid = _int(part_id)
    q = _float(qty)
    if not pid or not q:
        return _redirect("/stock", err="请选择物料并填写数量")
    try:
        stock_in(db, pid, _int(location_id), q, lot_no=lot_no, date_code=date_code,
                 unit_price=_float(price), note=note)
        return _redirect("/stock", msg=f"已入库 {q:g}")
    except Exception as exc:
        return _redirect("/stock", err=str(exc))


@app.post("/stock/out")
def stock_out_page(
    part_id: str = Form(""),
    qty: str = Form(""),
    location_id: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    pid = _int(part_id)
    q = _float(qty)
    if not pid or not q:
        return _redirect("/stock", err="请选择物料并填写数量")
    try:
        stock_out(db, pid, q, location_id=_int(location_id), note=note)
        return _redirect("/stock", msg=f"已出库 {q:g}")
    except Exception as exc:
        return _redirect("/stock", err=str(exc))


def _stocktake_url(location_id: str = "", q: str = "", include_children: str = "1") -> str:
    params = []
    if (location_id or "").strip():
        params.append("location_id=" + quote(str(location_id).strip()))
    if (q or "").strip():
        params.append("q=" + quote(str(q).strip()))
    if (include_children or "1") != "1":
        params.append("include_children=0")
    return "/stocktake" + ("?" + "&".join(params) if params else "")


@app.get("/stocktake", response_class=HTMLResponse)
def stocktake_page(
    request: Request,
    location_id: str = "",
    q: str = "",
    include_children: str = "1",
    db: Session = Depends(get_db),
):
    lid = _int(location_id)
    location_ids = None
    if lid:
        location_ids = {lid}
        if (include_children or "1") != "0":
            location_ids |= _location_descendant_ids(db, lid)

    query = (
        db.query(StockLot, Part)
        .join(Part, Part.id == StockLot.part_id)
        .filter(StockLot.qty > 0, Part.status == "active")
    )
    if location_ids:
        query = query.filter(StockLot.location_id.in_(location_ids))
    text = (q or "").strip()
    if text:
        like = f"%{text}%"
        source_ids = select(PartSource.part_id).where(PartSource.supplier_part_no.ilike(like))
        query = query.filter(or_(
            Part.model.ilike(like), Part.name.ilike(like), Part.code.ilike(like),
            Part.supplier_part_no.ilike(like), Part.id.in_(source_ids),
        ))
    limit = 1000
    raw_rows = (
        query.order_by(Part.model.asc(), StockLot.received_at.asc(), StockLot.id.asc())
        .limit(limit + 1)
        .all()
    )
    truncated = len(raw_rows) > limit
    raw_rows = raw_rows[:limit]
    lm = _loc_map(db)
    rows = [
        {"lot": lot, "part": part, "loc": lm.get(lot.location_id, "未分配")}
        for lot, part in raw_rows
    ]
    total_qty = sum(float(row["lot"].qty or 0) for row in rows)
    return templates.TemplateResponse(
        request,
        "stocktake.html",
        _ctx(
            request, db, active="stocktake", rows=rows, q=q,
            location_id=str(lid or ""), include_children=include_children,
            locations=_location_options(db), total_qty=total_qty, truncated=truncated,
            max_rows=limit,
        ),
    )


@app.post("/stocktake/apply")
def stocktake_apply(
    lot_id: list[str] = Form([]),
    actual: list[str] = Form([]),
    note: str = Form(""),
    location_id: str = Form(""),
    q: str = Form(""),
    include_children: str = Form("1"),
    db: Session = Depends(get_db),
):
    back = _stocktake_url(location_id, q, include_children)
    if len(lot_id) != len(actual):
        return _redirect(back, err="盘点数据不完整，请刷新后重试")
    try:
        result = apply_stocktake(db, list(zip(lot_id, actual)), note=note)
    except Exception as exc:
        return _redirect(back, err=str(exc))
    if result["count"] == 0:
        return _redirect(back, msg="账面数量没有变化，无需调整")
    detail = []
    if result["surplus"]:
        detail.append(f"盘盈 {result['surplus']} 个批次")
    if result["shortage"]:
        detail.append(f"盘亏 {result['shortage']} 个批次")
    suffix = "，".join(detail)
    return _redirect(
        back,
        msg=f"已调整 {result['count']} 个批次" + (f"（{suffix}）" if suffix else ""),
    )


@app.get("/stock/scan", response_class=HTMLResponse)
def stock_scan_page(
    request: Request,
    mode: str = "borrow",
    part_id: str = "",
    db: Session = Depends(get_db),
):
    if mode not in ("in", "out", "borrow", "return"):
        mode = "borrow"
    initial_part = None
    pid = _int(part_id)
    if pid:
        initial_part = db.get(Part, pid)
    open_rows = _loan_rows(db, loan_service.open_loans(db, limit=12))
    phone_token = _new_phone_scan_session()
    phone_url = f"{_phone_base_url(request)}/m/scan/{phone_token}"
    return templates.TemplateResponse(
        request,
        "stock_scan.html",
        _ctx(
            request, db, active="stock_scan", scan_mode=mode, initial_part=initial_part,
            open_loans=open_rows, open_loan_qty=loan_service.open_loan_qty(db),
            phone_token=phone_token, phone_url=phone_url, phone_qr=_qr_data_url(phone_url),
        ),
    )


@app.get("/m/scan/{token}", response_class=HTMLResponse)
def phone_scan_page(request: Request, token: str, db: Session = Depends(get_db)):
    valid = _get_phone_scan_session(token) is not None
    return templates.TemplateResponse(
        request,
        "phone_scan.html",
        _ctx(
            request, db, active="", phone_token=token, phone_valid=valid,
            phone_url=f"{_phone_base_url(request)}/m/scan/{token}",
        ),
        status_code=200 if valid else 404,
    )


@app.post("/api/scan-phone/{token}/hello")
def api_phone_scan_hello(token: str):
    sess = _get_phone_scan_session(token)
    if sess is None:
        return JSONResponse({"error": "配对已失效"}, status_code=404)
    sess["last_seen"] = time.time()
    return {"ok": True, "desktop_online": _phone_desktop_online(sess)}


@app.get("/api/scan-phone/{token}/next")
def api_phone_scan_next(token: str, after: int = 0):
    sess = _get_phone_scan_session(token)
    if sess is None:
        return JSONResponse({"error": "配对已失效"}, status_code=404)
    sess["last_poll"] = time.time()
    events = [e for e in sess["events"] if int(e["id"]) > max(0, after)]
    return {
        "ok": True,
        "online": time.time() - float(sess.get("last_seen", 0)) < 20,
        "events": events,
    }


@app.post("/api/scan-phone/{token}/close")
def api_phone_scan_close(token: str):
    _PHONE_SCANS.pop((token or "").strip(), None)
    return {"ok": True}


@app.post("/api/scan-phone/{token}/upload")
async def api_phone_scan_upload(token: str, file: UploadFile = File(...)):
    sess = _get_phone_scan_session(token)
    if sess is None:
        return JSONResponse({"error": "配对已失效，请在电脑上刷新扫码页后重新扫码"}, status_code=404)
    if not _phone_desktop_online(sess):
        return JSONResponse({"error": "电脑扫码页未连接，请在电脑上刷新后重新扫码"}, status_code=409)
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        return JSONResponse({"error": "图片太大，请重新拍摄"}, status_code=413)
    codes = ocr.decode_codes_bytes(data)
    if not codes:
        return JSONResponse({"error": "没有识别到二维码/条码，请靠近并对准再试"}, status_code=422)
    code = _phone_code(codes)
    event = _queue_phone_scan(token, code)
    sess["last_seen"] = time.time()
    return {"ok": True, "code": code, "codes": codes, "duplicate": event is None, "event": event}


@app.post("/api/scan-phone/{token}/push")
def api_phone_scan_push(token: str, code: str = Form("")):
    sess = _get_phone_scan_session(token)
    if sess is None:
        return JSONResponse({"error": "配对已失效，请在电脑上刷新扫码页后重新扫码"}, status_code=404)
    if not _phone_desktop_online(sess):
        return JSONResponse({"error": "电脑扫码页未连接，请在电脑上刷新后重新扫码"}, status_code=409)
    c = (code or "").strip()
    if not c:
        return JSONResponse({"error": "请输入条码内容"}, status_code=400)
    event = _queue_phone_scan(token, c[:300])
    sess["last_seen"] = time.time()
    return {"ok": True, "code": c[:300], "duplicate": event is None, "event": event}


@app.post("/api/decode-code")
async def api_decode_code(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"error": "没有收到图片"}, status_code=400)
    if len(data) > 12 * 1024 * 1024:
        return JSONResponse({"error": "图片太大，请重新拍摄"}, status_code=413)
    codes = ocr.decode_codes_bytes(data)
    if not codes:
        return {"ok": False, "code": "", "codes": [], "error": "没有识别到二维码/条码，请靠近并对准再试"}
    return {"ok": True, "code": _phone_code(codes), "codes": codes[:10]}


@app.post("/api/stock-scan/execute")
def api_stock_scan_execute(
    action: str = Form(""),
    part_id: str = Form(""),
    location_id: str = Form(""),
    qty: str = Form(""),
    borrower: str = Form(""),
    note: str = Form(""),
    lcsc_code: str = Form(""),
    db: Session = Depends(get_db),
):
    pid = _int(part_id)
    lid = _int(location_id)
    q = _float(qty)
    if not lid:
        return JSONResponse({"error": "请扫描库位签"}, status_code=400)
    if not q or q <= 0:
        return JSONResponse({"error": "数量必须大于 0"}, status_code=400)
    loc = db.get(Location, lid)
    if not loc:
        return JSONResponse({"error": "库位不存在"}, status_code=404)
    path = location_path(db, loc)
    created = False
    code = lcsc.extract_code(lcsc_code)
    try:
        if not pid:
            if not code:
                return JSONResponse({"error": "请先扫描物品签"}, status_code=400)
            if action != "in":
                return JSONResponse({"error": "立创 C 码自动建档仅支持入库"}, status_code=400)
            p = _find_part(db, code)
            if p is None:
                candidate = lcsc.lookup(code)
                p = lcsc.create_part_from_candidate(
                    db,
                    candidate,
                    location_id=lid,
                    note=note.strip() or f"立创扫码自动建档（{code}）",
                )
                created = True
            pid = p.id
        p = db.get(Part, pid)
        if not p:
            return JSONResponse({"error": "物料不存在"}, status_code=404)
        if action == "in":
            stock_in(
                db, pid, lid, q,
                note=note.strip() or ("立创扫码入库" if created else "扫码入库"),
                supplier_id=p.supplier_id,
                ref_type="scan",
            )
            message = (
                f"已创建立创物料 {p.model} 并入库 {q:g} 到 {path}"
                if created else f"已入库 {q:g} 到 {path}"
            )
        elif action == "out":
            stock_out(db, pid, q, location_id=lid, note=note.strip() or "扫码出库", ref_type="scan")
            message = f"已从 {path} 出库 {q:g}"
        elif action == "borrow":
            loan_service.borrow(db, pid, lid, q, borrower=borrower, note=note)
            message = f"已从 {path} 借出 {q:g}" + (f"（{borrower.strip()}）" if borrower.strip() else "")
        elif action == "return":
            loan_service.return_loans(db, pid, lid, q, note=note)
            message = f"已归还 {q:g} 到 {path}"
        else:
            return JSONResponse({"error": "未知操作"}, status_code=400)
    except lcsc.LcscError as exc:
        db.rollback()
        return JSONResponse({"error": f"立创查询失败：{exc}"}, status_code=502)
    except Exception as exc:
        db.rollback()
        return JSONResponse({"error": str(exc)}, status_code=400)
    open_qty = loan_service.open_loan_qty(db, part_id=pid)
    return {
        "ok": True,
        "message": message,
        "created": created,
        "part_id": pid,
        "part_model": p.model,
        "part_qty": part_total_qty(db, pid),
        "open_qty": open_qty,
        "open_loan_count": loan_service.open_loan_count(db),
    }


@app.get("/api/stock-scan/lookup")
def api_stock_scan_lookup(
    code: str = "",
    expect: str = "",
    mode: str = "",
    db: Session = Depends(get_db),
):
    c = (code or "").strip()
    if not c:
        return JSONResponse({"error": "空码"}, status_code=400)
    # 立创包装袋条码可能直接是 C1525，也可能带 LCSC: 前缀或 URL 参数。
    lcsc_code = lcsc.extract_code(c)

    def lookup_location_only():
        suffix = c[5:] if c.upper().startswith("PV-L-") else c
        loc = _lookup_location(db, suffix)
        if loc:
            return _location_scan_payload(db, loc)
        return None

    def lookup_part_only():
        p = _find_part(db, c)
        if p is None and lcsc_code and lcsc_code.lower() != c.lower():
            p = _find_part(db, lcsc_code)
        if p:
            return _part_scan_payload(db, p)
        return None

    if expect == "location":
        result = lookup_location_only()
        if result:
            return result
        if c.upper().startswith("PV-P-") or _find_part(db, c):
            return JSONResponse({"error": "这是物品签，请先扫描物品，再扫描库位签"}, status_code=400)
        return JSONResponse({"error": "没有找到这个库位"}, status_code=404)

    if expect == "part":
        result = lookup_part_only()
        if result:
            return result
        if c.upper().startswith("PV-L-") or _lookup_location(db, c[5:] if c.upper().startswith("PV-L-") else c):
            return JSONResponse({"error": "这是库位签，请先扫描物品签"}, status_code=400)
        if lcsc_code:
            if mode != "in":
                return JSONResponse(
                    {"error": "这是立创 C 码；请切换到“入库”后再扫，或先在“拍照入库”中建档"},
                    status_code=400,
                )
            try:
                candidate = lcsc.lookup(lcsc_code)
            except lcsc.LcscError as exc:
                return JSONResponse({"error": f"立创查询失败：{exc}"}, status_code=502)
            return _lcsc_part_scan_payload(candidate)
        return JSONResponse({"error": "没有找到这个物品"}, status_code=404)

    result = lookup_part_only()
    if result:
        return result
    result = lookup_location_only()
    if result:
        return result
    return JSONResponse({"error": "没有找到对应物料或库位"}, status_code=404)


@app.get("/api/loans/open")
def api_open_loans(limit: int = 12, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 50))
    loans = loan_service.open_loans(db, limit=limit)
    rows = _loan_rows(db, loans)
    return {
        "count": loan_service.open_loan_count(db),
        "qty": loan_service.open_loan_qty(db),
        "loans": [
            {
                "id": row["loan"].id,
                "part_id": row["part"].id if row["part"] else None,
                "model": row["part"].model if row["part"] else "已删除物料",
                "code": row["part"].code if row["part"] else "",
                "qty": float(row["loan"].qty or 0),
                "returned_qty": float(row["loan"].returned_qty or 0),
                "outstanding": float(row["loan"].outstanding_qty),
                "borrower": row["loan"].borrower or "",
                "out_location": row["out_path"],
                "created_at": row["loan"].created_at.strftime("%Y-%m-%d %H:%M") if row["loan"].created_at else "",
            }
            for row in rows
        ],
    }


@app.get("/loans", response_class=HTMLResponse)
def loans_page(
    request: Request,
    status: str = "open",
    q: str = "",
    db: Session = Depends(get_db),
):
    if status not in ("open", "returned", "all"):
        status = "open"
    query = db.query(Loan).join(Part, Loan.part_id == Part.id)
    if status == "open":
        query = query.filter(Loan.status == "open", Loan.returned_qty < Loan.qty)
    elif status == "returned":
        query = query.filter(Loan.status == "returned")
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Part.model.ilike(like), Part.name.ilike(like), Part.code.ilike(like),
            Part.supplier_part_no.ilike(like), Loan.borrower.ilike(like),
        ))
    loans = query.order_by(Loan.status.asc(), Loan.created_at.desc(), Loan.id.desc()).limit(500).all()
    rows = _loan_rows(db, loans)
    return templates.TemplateResponse(
        request,
        "loans.html",
        _ctx(
            request, db, active="loans", status=status, q=q, loans=rows,
            open_count=loan_service.open_loan_count(db),
            open_qty=loan_service.open_loan_qty(db),
        ),
    )


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "scan.html", _ctx(request, db, active="scan"))


@app.get("/api/resolve")
def api_resolve(code: str = "", db: Session = Depends(get_db)):
    c = (code or "").strip()
    if not c:
        return JSONResponse({"error": "空码"}, status_code=400)
    if c.startswith("PV-L-"):
        suffix = c[5:]
        loc = _lookup_location(db, suffix)
        if loc:
            return {"kind": "location", "id": loc.id, "url": f"/locations/{loc.id}", "title": f"{loc.code} · {loc.name}"}
    p = _find_part(db, c)
    if p:
        return {"kind": "part", "id": p.id, "url": f"/parts/{p.id}", "title": p.model}
    loc = _lookup_location(db, c)
    if loc:
        return {"kind": "location", "id": loc.id, "url": f"/locations/{loc.id}", "title": f"{loc.code} · {loc.name}"}
    return JSONResponse({"error": "没有找到对应物料或库位"}, status_code=404)


@app.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "intake.html",
        _ctx(request, db, active="intake", categories=_categories(db), suppliers=_suppliers(db),
             locations=_location_options(db)),
    )


@app.get("/api/lcsc/search")
def api_lcsc_search(
    q: str = "",
    package: str = "",
    brand: str = "",
    limit: int = 20,
    refresh: int = 0,
):
    try:
        return lcsc.search(q, package=package, brand=brand, limit=limit, refresh=bool(refresh))
    except lcsc.LcscError as exc:
        return JSONResponse(
            {"error": str(exc), "manual_url": lcsc.manual_url(q)},
            status_code=502,
        )


@app.post("/api/ocr")
async def api_ocr(file: UploadFile = File(...), db: Session = Depends(get_db)):
    name = _save_upload(file, "ocr")
    if not name:
        return JSONResponse({"error": "没有收到图片"}, status_code=400)
    path = UPLOAD_DIR / name
    try:
        result = ocr.recognize(str(path))
    except Exception as exc:
        return JSONResponse({"error": f"识别失败：{exc}", "image": name, "image_url": f"/uploads/{name}"}, status_code=500)
    fields = result["fields"]
    matches = _match_parts(db, fields)
    job = OcrJob(image_path=name, raw_text=result["raw_text"],
                 parsed_json=json.dumps(result, ensure_ascii=False), status="pending")
    db.add(job)
    db.commit()
    return {"image": name, "image_url": f"/uploads/{name}", "fields": fields,
            "raw_text": result["raw_text"], "lines": result["lines"], "matches": matches,
            "source": result.get("source"), "confidence": result.get("confidence", {}),
            "evidence": result.get("evidence", {}), "qty_detail": result.get("qty_detail", {}),
            "job_id": job.id}


def _match_parts(db, fields: dict) -> list[dict]:
    """按料号 > 型号 > 名称的顺序找已有物料，并给出匹配原因。"""
    found: dict[int, dict] = {}

    def add(p, score: int, reason: str):
        if p is None or p.id in found:
            return
        brief = _part_brief(db, p)
        brief["match_score"] = score
        brief["match_reason"] = reason
        found[p.id] = brief

    part_no = (fields.get("supplier_part_no") or "").strip()
    if part_no:
        for p in db.query(Part).filter(func.lower(Part.supplier_part_no) == part_no.lower()).limit(8):
            add(p, 100, "供应商料号一致")
        for source in db.query(PartSource).filter(
            func.lower(PartSource.supplier_part_no) == part_no.lower()
        ).limit(8):
            add(db.get(Part, source.part_id), 100, "来源料号一致")

    model = (fields.get("model") or "").strip()
    if model:
        for p in db.query(Part).filter(Part.model.ilike(model)).limit(8):
            add(p, 95, "型号一致")
        for p in db.query(Part).filter(Part.model.ilike(f"%{model}%")).limit(8):
            add(p, 80, "型号包含")

    name = (fields.get("name") or "").strip()
    if name:
        needle = name[:40]
        for p in db.query(Part).filter(Part.name.ilike(f"%{needle}%")).limit(8):
            add(p, 60, "名称包含")

    items = list(found.values())
    items.sort(key=lambda x: (-x["match_score"], x["id"]))
    return items[:8]


def _part_brief(db, p):
    qty = part_total_qty(db, p.id)
    locs = db.query(StockLot.location_id, func.sum(StockLot.qty)) \
        .filter(StockLot.part_id == p.id, StockLot.qty > 0) \
        .group_by(StockLot.location_id).all()
    loc_names = []
    for lid, _q in locs:
        loc = db.get(Location, lid) if lid else None
        path = location_path(db, loc) if loc else "未分配"
        if path not in loc_names:
            loc_names.append(path)
    return {"id": p.id, "code": p.code, "model": p.model, "name": p.name,
            "package": p.package, "qty": qty, "loc": "、".join(loc_names),
            "open_qty": loan_service.open_loan_qty(db, part_id=p.id)}


@app.get("/api/parts/search")
def api_part_search(q: str = "", db: Session = Depends(get_db)):
    query = db.query(Part).filter(Part.status == "active")
    if q.strip():
        like = f"%{q.strip()}%"
        source_ids = select(PartSource.part_id).where(PartSource.supplier_part_no.ilike(like))
        query = query.filter(or_(Part.model.ilike(like), Part.name.ilike(like),
                                 Part.supplier_part_no.ilike(like), Part.code.ilike(like),
                                 Part.id.in_(source_ids)))
    parts = query.order_by(Part.updated_at.desc()).limit(20).all()
    return [_part_brief(db, p) for p in parts]


@app.post("/intake/confirm")
def intake_confirm(
    mode: str = Form("new"),
    part_id: str = Form(""),
    model: str = Form(""),
    name: str = Form(""),
    brand: str = Form(""),
    package: str = Form(""),
    category_id: str = Form(""),
    supplier_id: str = Form(""),
    supplier_part_no: str = Form(""),
    location_id: str = Form(""),
    qty: str = Form(""),
    price: str = Form(""),
    lot_no: str = Form(""),
    date_code: str = Form(""),
    note: str = Form(""),
    image: str = Form(""),
    job_id: str = Form(""),
    lcsc_code: str = Form(""),
    lcsc_mode: str = Form("blank"),
    db: Session = Depends(get_db),
):
    q = _float(qty)
    lid = _int(location_id)
    if not q or q <= 0:
        return _redirect("/intake", err="请填写入库数量")
    if not lid:
        return _redirect("/intake", err="请选择库位")
    pid = _int(part_id)
    if mode == "existing":
        if not pid:
            return _redirect("/intake", err="请先选择已有物料，或切换到新建物料")
        p = db.get(Part, pid)
        if not p:
            return _redirect("/intake", err="物料不存在")
    else:
        if not model.strip():
            return _redirect("/intake", err="新型号不能为空")
        p = Part(
            code=next_part_code(db),
            model=model.strip()[:200],
            name=name.strip()[:300] or model.strip()[:300],
            brand=brand.strip()[:120],
            package=package.strip()[:80],
            category_id=_int(category_id),
            supplier_id=_int(supplier_id),
            supplier_part_no=supplier_part_no.strip()[:120],
            unit="PCS",
        )
        p.note = note.strip()
        db.add(p)
        db.flush()
    lcsc_warn = _apply_lcsc_code(db, p, lcsc_code, lcsc_mode)
    try:
        stock_in(db, p.id, lid, q, lot_no=lot_no, date_code=date_code,
                 unit_price=_float(price), supplier_id=_int(supplier_id) or p.supplier_id,
                 note=note or "OCR 入库", ref_type="ocr")
    except Exception as exc:
        return _redirect("/intake", err=str(exc))
    if image:
        db.add(Attachment(part_id=p.id, kind="screenshot", path=image))
    jid = _int(job_id)
    if jid:
        job = db.get(OcrJob, jid)
        if job:
            job.status = "confirmed"
            job.part_id = p.id
    db.commit()
    msg = f"已入库 {q:g}" + (f"；{lcsc_warn}" if lcsc_warn else "")
    return _redirect(f"/parts/{p.id}", msg=msg)


@app.get("/labels", response_class=HTMLResponse)
def labels_page(request: Request, ids: str = "", q: str = "", db: Session = Depends(get_db)):
    query = db.query(Part).filter(Part.status == "active")
    if q.strip():
        like = f"%{q.strip()}%"
        source_ids = select(PartSource.part_id).where(PartSource.supplier_part_no.ilike(like))
        query = query.filter(or_(Part.model.ilike(like), Part.name.ilike(like),
                                 Part.supplier_part_no.ilike(like), Part.code.ilike(like),
                                 Part.id.in_(source_ids)))
    parts = _attach_part_meta(db, query.order_by(Part.updated_at.desc()).limit(300).all())
    selected = {_int(x) for x in ids.split(",") if _int(x)}
    selected_parts = _selected_parts(db, sorted(selected))
    visible_ids = {p.id for p in parts}
    extra_selected = [p for p in selected_parts if p.id not in visible_ids]
    return templates.TemplateResponse(
        request,
        "labels.html",
        _ctx(request, db, active="labels", parts=parts, q=q, selected=selected,
             selected_parts=selected_parts, extra_selected=extra_selected,
             categories=_categories(db)),
    )


def _selected_parts(db: Session, ids: list[int]):
    if not ids:
        return []
    parts = db.query(Part).filter(Part.id.in_(ids)).all()
    order = {pid: i for i, pid in enumerate(ids)}
    parts.sort(key=lambda p: order.get(p.id, 9999))
    return _attach_part_meta(db, parts)


@app.post("/labels/pdf")
async def labels_pdf(
    request: Request,
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    show_price: str = Form(""),
    db: Session = Depends(get_db),
):
    parts = _selected_parts(db, ids)
    if not parts:
        return _redirect("/labels", err="请先勾选物料")
    pdf = labels.build_pdf(parts, brand=brand or get_setting(db, "label_brand", "元件库"),
                           show_price=bool(show_price),
                           kind=get_setting(db, "label_kind", "元件标签"))
    fname = f"labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    (EXPORT_DIR / fname).write_bytes(pdf)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/labels/sef")
async def labels_sef(
    request: Request,
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    parts = _selected_parts(db, ids)
    if not parts:
        return _redirect("/labels", err="请先勾选物料")
    data = labels.build_sef(parts, brand=brand or get_setting(db, "label_brand", "元件库"),
                            kind=get_setting(db, "label_kind", "元件标签"))
    fname = f"labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sef"
    (EXPORT_DIR / fname).write_bytes(data)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/labels/printer-status")
def labels_printer_status():
    return JSONResponse(
        printer_service.status(),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/labels/direct-print")
def labels_direct_print(
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    show_price: str = Form(""),
    db: Session = Depends(get_db),
):
    parts = _selected_parts(db, ids)
    if not parts:
        return _redirect("/labels", err="请先勾选物料")
    result = printer_service.print_parts(
        parts,
        brand=brand or get_setting(db, "label_brand", "元件库"),
        show_price=bool(show_price),
        kind=get_setting(db, "label_kind", "元件标签"),
    )
    keep = f"/labels?ids={','.join(str(p.id) for p in parts)}"
    if result.get("ok"):
        msg = f"已发送 {len(parts)} 张标签到打印机"
        state = result.get("state")
        if state and state not in ("Waiting", "Completed"):
            state_text = {
                "CheckDevice": "检测设备",
                "Printting": "打印中",
                "AbortPrint": "已中止",
                "ResetDevice": "复位中",
            }.get(state, state)
            msg += f"（{state_text}）"
        return _redirect(keep, msg=msg)
    err = result.get("error") or result.get("errorMsg") or result.get("printDes") or "打印失败"
    return _redirect(keep, err=str(err))


def _locations_with_meta(db: Session, q: str = ""):
    query = db.query(Location)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Location.code.ilike(like), Location.name.ilike(like)))
    locs = query.order_by(Location.sort.asc(), Location.code.asc()).all()
    counts = dict(db.query(StockLot.location_id, func.count(func.distinct(StockLot.part_id)))
                  .filter(StockLot.qty > 0).group_by(StockLot.location_id).all())
    qty = dict(db.query(StockLot.location_id, func.sum(StockLot.qty)).group_by(StockLot.location_id).all())
    for l in locs:
        l._path = location_path(db, l)
        l._count = int(counts.get(l.id, 0))
        l._qty = float(qty.get(l.id, 0) or 0)
    return locs


@app.get("/location-labels", response_class=HTMLResponse)
def location_labels_page(request: Request, ids: str = "", q: str = "", db: Session = Depends(get_db)):
    locs = _locations_with_meta(db, q)
    selected = {_int(x) for x in ids.split(",") if _int(x)}
    selected_locations = _selected_locations(db, sorted(selected))
    visible_ids = {l.id for l in locs}
    extra_selected = [l for l in selected_locations if l.id not in visible_ids]
    return templates.TemplateResponse(
        request,
        "location_labels.html",
        _ctx(request, db, active="labels", locations=locs, selected=selected, q=q,
             selected_locations=selected_locations, extra_selected=extra_selected),
    )


def _selected_locations(db: Session, ids: list[int]):
    if not ids:
        return []
    locs = db.query(Location).filter(Location.id.in_(ids)).all()
    order = {lid: i for i, lid in enumerate(ids)}
    locs.sort(key=lambda l: order.get(l.id, 9999))
    for l in locs:
        l._path = location_path(db, l)
    return locs


@app.post("/location-labels/pdf")
async def location_labels_pdf(
    request: Request,
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    locs = _selected_locations(db, ids)
    if not locs:
        return _redirect("/location-labels", err="请先勾选库位")
    pdf = labels.build_location_pdf(locs, brand=brand or get_setting(db, "label_brand", "元件库"))
    fname = f"location_labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    (EXPORT_DIR / fname).write_bytes(pdf)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/location-labels/sef")
async def location_labels_sef(
    request: Request,
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    locs = _selected_locations(db, ids)
    if not locs:
        return _redirect("/location-labels", err="请先勾选库位")
    data = labels.build_location_sef(
        locs,
        brand=brand or get_setting(db, "label_brand", "元件库"),
        kind=get_setting(db, "location_label_kind", "库位标签"),
    )
    fname = f"location_labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sef"
    (EXPORT_DIR / fname).write_bytes(data)
    return Response(content=data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/location-labels/direct-print")
def location_labels_direct_print(
    ids: list[int] = Form(default=[]),
    brand: str = Form(""),
    db: Session = Depends(get_db),
):
    locs = _selected_locations(db, ids)
    if not locs:
        return _redirect("/location-labels", err="请先勾选库位")
    result = printer_service.print_locations(
        locs,
        brand=brand or get_setting(db, "label_brand", "元件库"),
        kind=get_setting(db, "location_label_kind", "库位标签"),
    )
    keep = f"/location-labels?ids={','.join(str(l.id) for l in locs)}"
    if result.get("ok"):
        msg = f"已发送 {len(locs)} 张库位标签到打印机"
        state = result.get("state")
        if state and state not in ("Waiting", "Completed"):
            state_text = {
                "CheckDevice": "检测设备",
                "Printting": "打印中",
                "AbortPrint": "已中止",
                "ResetDevice": "复位中",
            }.get(state, state)
            msg += f"（{state_text}）"
        return _redirect(keep, msg=msg)
    err = result.get("error") or result.get("errorMsg") or result.get("printDes") or "打印失败"
    return _redirect(keep, err=str(err))


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "import.html",
        _ctx(request, db, active="import", locations=_location_options(db), suppliers=_suppliers(db),
             result=None),
    )


@app.post("/import/preview", response_class=HTMLResponse)
def import_preview(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    name = _save_upload(file, "import")
    if not name:
        return _redirect("/import", err="请选择文件")
    path = UPLOAD_DIR / name
    try:
        headers, rows = importer.read_table(path)
    except Exception as exc:
        return _redirect("/import", err=f"读取失败：{exc}")
    mapping = importer.auto_map(headers)
    preview = rows[:8]
    return templates.TemplateResponse(
        request,
        "import.html",
        _ctx(request, db, active="import", locations=_location_options(db), suppliers=_suppliers(db),
             result=None, file_name=name, headers=headers, mapping=mapping, preview=preview,
             total_rows=len(rows)),
    )


@app.post("/import/commit", response_class=HTMLResponse)
def import_commit(
    request: Request,
    file_name: str = Form(""),
    location_id: str = Form(""),
    supplier_id: str = Form(""),
    map_supplier_part_no: str = Form(""),
    map_model: str = Form(""),
    map_name: str = Form(""),
    map_brand: str = Form(""),
    map_package: str = Form(""),
    map_qty: str = Form(""),
    map_price: str = Form(""),
    map_lot_no: str = Form(""),
    map_date_code: str = Form(""),
    map_location_code: str = Form(""),
    db: Session = Depends(get_db),
):
    if not file_name:
        return _redirect("/import", err="找不到上传文件，请重新上传")
    path = UPLOAD_DIR / file_name
    if not path.exists():
        return _redirect("/import", err="临时文件已失效，请重新上传")
    lid = _int(location_id)
    if not lid:
        return _redirect("/import", err="请选择入库库位")
    headers, rows = importer.read_table(path)
    raw_map = {
        "supplier_part_no": map_supplier_part_no,
        "model": map_model,
        "name": map_name,
        "brand": map_brand,
        "package": map_package,
        "qty": map_qty,
        "price": map_price,
        "lot_no": map_lot_no,
        "date_code": map_date_code,
        "location_code": map_location_code,
    }
    mapping = {k: int(v) for k, v in raw_map.items() if v not in ("", None)}
    result = importer.commit_rows(db, rows, mapping, lid, _int(supplier_id))
    return templates.TemplateResponse(
        request,
        "import.html",
        _ctx(request, db, active="import", locations=_location_options(db), suppliers=_suppliers(db),
             result=result, file_name="", headers=[], mapping={}, preview=[], total_rows=0),
    )


@app.get("/import/template")
def import_template():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "元件数据"
    headers = ["料号", "型号", "名称", "品牌", "封装", "数量", "单价", "批次", "日期码", "库位"]
    ws.append(headers)
    ws.append(["C1525", "100nF 50V X7R", "贴片电容 100nF 50V X7R 0603", "三星", "0603", 4000, 0.012, "2026-10-21", "2026-10-21", "A-01-02"])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(content=buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="import_template.xlsx"'})


@app.get("/bom", response_class=HTMLResponse)
def bom_list(request: Request, db: Session = Depends(get_db)):
    projects = db.query(BomProject).order_by(BomProject.created_at.desc()).all()
    counts = dict(db.query(BomItem.project_id, func.count(BomItem.id)).group_by(BomItem.project_id).all())
    return templates.TemplateResponse(
        request, "bom.html", _ctx(request, db, active="bom", projects=projects, counts=counts),
    )


@app.post("/bom/upload")
def bom_upload(
    name: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    saved = _save_upload(file, "bom")
    if not saved:
        return _redirect("/bom", err="请选择 BOM 文件")
    try:
        headers, rows = importer.read_table(UPLOAD_DIR / saved)
    except Exception as exc:
        return _redirect("/bom", err=f"读取 BOM 失败：{exc}")
    mapping = importer.auto_map(headers)
    project = BomProject(name=(name.strip() or Path(file.filename or "BOM").stem)[:200])
    db.add(project)
    db.flush()
    created = 0
    for row in rows:
        def get(field):
            idx = mapping.get(field)
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx]).strip() if row[idx] is not None else ""

        model = get("model") or get("name")
        qty = _float(get("qty"), 0) or 0
        if not model and not get("supplier_part_no"):
            continue
        spn = get("supplier_part_no")
        part = None
        if spn:
            part = db.query(Part).filter(func.lower(Part.supplier_part_no) == spn.lower()).first()
            if part is None:
                source = db.query(PartSource).filter(
                    func.lower(PartSource.supplier_part_no) == spn.lower()
                ).first()
                if source:
                    part = db.get(Part, source.part_id)
        if part is None and model:
            part = db.query(Part).filter(Part.model == model).first()
        if part is None and model:
            part = db.query(Part).filter(Part.model.ilike(f"%{model}%")).first()
        db.add(BomItem(
            project_id=project.id,
            part_id=part.id if part else None,
            model=(model or spn)[:200],
            qty=qty,
            designators=get("designators")[:300],
            matched=bool(part),
        ))
        created += 1
    db.commit()
    return _redirect(f"/bom/{project.id}", msg=f"已导入 {created} 行")


@app.get("/bom/{project_id}", response_class=HTMLResponse)
def bom_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
    project = db.get(BomProject, project_id)
    if not project:
        return _redirect("/bom", err="BOM 不存在")
    items = db.query(BomItem).filter(BomItem.project_id == project_id).order_by(BomItem.id.asc()).all()
    qm = _qty_map(db)
    rows = []
    for it in items:
        p = db.get(Part, it.part_id) if it.part_id else None
        stock = qm.get(p.id, 0) if p else 0
        ok = bool(p) and stock + 1e-9 >= (it.qty or 0)
        rows.append({"item": it, "part": p, "stock": stock, "ok": ok})
    matched = sum(1 for r in rows if r["part"])
    short = sum(1 for r in rows if not r["ok"])
    parts = (
        db.query(Part)
        .filter(Part.status == "active")
        .order_by(Part.model.asc(), Part.id.asc())
        .all()
    )
    return templates.TemplateResponse(
        request, "bom_detail.html",
        _ctx(request, db, active="bom", project=project, rows=rows, matched=matched,
             short=short, parts=parts),
    )


@app.post("/bom/{project_id}/items/{item_id}/match")
def bom_match_item(
    project_id: int,
    item_id: int,
    part_query: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        item = bom_service.assign_item(db, project_id, item_id, part_query=part_query)
    except bom_service.BomError as exc:
        return _redirect(f"/bom/{project_id}", err=str(exc))
    part = db.get(Part, item.part_id)
    return _redirect(
        f"/bom/{project_id}",
        msg=f"已把 BOM 行「{item.model or '?'}」匹配到 {part.model if part else '物料'}",
    )


@app.post("/bom/{project_id}/items/{item_id}/unmatch")
def bom_unmatch_item(project_id: int, item_id: int, db: Session = Depends(get_db)):
    try:
        bom_service.clear_item(db, project_id, item_id)
    except bom_service.BomError as exc:
        return _redirect(f"/bom/{project_id}", err=str(exc))
    return _redirect(f"/bom/{project_id}", msg="已取消该行的物料匹配")


@app.post("/bom/{project_id}/out")
def bom_out(project_id: int, db: Session = Depends(get_db)):
    project = db.get(BomProject, project_id)
    if not project:
        return _redirect("/bom", err="BOM 不存在")
    items = db.query(BomItem).filter(BomItem.project_id == project_id).all()
    qm = _qty_map(db)
    done, skipped = 0, []
    for it in items:
        if not it.part_id or not it.qty:
            skipped.append(it.model or "?")
            continue
        if qm.get(it.part_id, 0) + 1e-9 < it.qty:
            skipped.append(it.model or "?")
            continue
        try:
            stock_out(db, it.part_id, it.qty, note=f"BOM 出库：{project.name}", ref_type="bom", ref_id=project.id)
            qm[it.part_id] = max(0, qm.get(it.part_id, 0) - it.qty)
            done += 1
        except Exception:
            skipped.append(it.model or "?")
    msg = f"已出库 {done} 项"
    if skipped:
        return _redirect(f"/bom/{project_id}", msg=msg + f"，{len(skipped)} 项库存不足/未匹配")
    return _redirect(f"/bom/{project_id}", msg=msg)


@app.post("/bom/{project_id}/delete")
def bom_delete(project_id: int, db: Session = Depends(get_db)):
    db.query(BomItem).filter(BomItem.project_id == project_id).delete()
    p = db.get(BomProject, project_id)
    if p:
        db.delete(p)
        db.commit()
    return _redirect("/bom", msg="已删除 BOM")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    counts = {
        "parts": db.query(Part).count(),
        "lots": db.query(StockLot).count(),
        "moves": db.query(StockMove).count(),
        "locations": db.query(Location).count(),
    }
    db_size = (DATA_DIR / "partvault.db").stat().st_size if (DATA_DIR / "partvault.db").exists() else 0
    suppliers = _suppliers(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(request, db, active="settings", categories=_categories(db), suppliers=suppliers,
             supplier_usage={s.id: supplier_service.supplier_usage(db, s.id) for s in suppliers},
             backups=backup_service.list_backups(),
             returns_report=repair.legacy_return_report(db),
             locations=_location_options(db), counts=counts, db_size=db_size,
             cat_map={c.id: c.name for c in _categories(db)}, data_dir=str(DATA_DIR),
             settings={s.key: s.value for s in db.query(Setting).all()}),
    )


@app.post("/settings/general")
def settings_general(
    site_name: str = Form(""),
    label_brand: str = Form(""),
    label_kind: str = Form(""),
    location_label_kind: str = Form(""),
    db: Session = Depends(get_db),
):
    set_setting(db, "site_name", site_name.strip() or "元件库管家")
    set_setting(db, "label_brand", label_brand.strip() or "元件库")
    set_setting(db, "label_kind", label_kind.strip() or "元件标签")
    set_setting(db, "location_label_kind", location_label_kind.strip() or "库位标签")
    db.commit()
    return _redirect("/settings", msg="已保存")


@app.post("/settings/category")
def category_create(name: str = Form(""), parent_id: str = Form(""), db: Session = Depends(get_db)):
    if not name.strip():
        return _redirect("/settings", err="分类名不能为空")
    db.add(Category(name=name.strip()[:80], parent_id=_int(parent_id)))
    db.commit()
    return _redirect("/settings", msg="已添加分类")


@app.post("/settings/category/{cat_id}/delete")
def category_delete(cat_id: int, db: Session = Depends(get_db)):
    if db.query(Part).filter(Part.category_id == cat_id).count():
        return _redirect("/settings", err="该分类下还有物料")
    if db.query(Category).filter(Category.parent_id == cat_id).count():
        return _redirect("/settings", err="请先删除子分类")
    c = db.get(Category, cat_id)
    if c:
        db.delete(c)
        db.commit()
    return _redirect("/settings", msg="已删除分类")


@app.post("/settings/supplier")
def supplier_create(
    name: str = Form(""),
    type: str = Form("other"),
    homepage: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        supplier = supplier_service.create_supplier(
            db, name=name, type=type, homepage=homepage, note=note,
        )
    except supplier_service.SupplierError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", msg=f"已添加供应商 {supplier.name}")


def _render_supplier_edit(
    request: Request,
    db: Session,
    supplier: Supplier,
    status_code: int = 200,
    error: str = "",
    **form_values,
):
    def pick(key, fallback):
        return form_values[key] if key in form_values else fallback

    form = {
        "name": str(pick("name", supplier.name or "") or ""),
        "type": str(pick("type", supplier.type or "other") or "other"),
        "homepage": str(pick("homepage", supplier.homepage or "") or ""),
        "note": str(pick("note", supplier.note or "") or ""),
    }
    return templates.TemplateResponse(
        request,
        "supplier_form.html",
        _ctx(
            request, db, active="settings", supplier=supplier, form=form,
            types=supplier_service.SUPPLIER_TYPES,
            type_labels=supplier_service.SUPPLIER_TYPE_LABELS,
            usage=supplier_service.supplier_usage(db, supplier.id),
            err=error,
        ),
        status_code=status_code,
    )


@app.get("/settings/supplier/{supplier_id}/edit", response_class=HTMLResponse)
def supplier_edit(supplier_id: int, request: Request, db: Session = Depends(get_db)):
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return _redirect("/settings", err="供应商不存在")
    return _render_supplier_edit(request, db, supplier)


@app.post("/settings/supplier/{supplier_id}/edit")
def supplier_update(
    supplier_id: int,
    request: Request,
    name: str = Form(""),
    type: str = Form("other"),
    homepage: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    supplier = db.get(Supplier, supplier_id)
    if not supplier:
        return _redirect("/settings", err="供应商不存在")
    try:
        updated = supplier_service.update_supplier(
            db, supplier.id, name=name, type=type, homepage=homepage, note=note,
        )
    except supplier_service.SupplierError as exc:
        return _render_supplier_edit(
            request, db, supplier, status_code=400, error=str(exc),
            name=name, type=type, homepage=homepage, note=note,
        )
    return _redirect("/settings", msg=f"已保存供应商 {updated.name}")


@app.post("/settings/supplier/{supplier_id}/delete")
def supplier_delete(supplier_id: int, db: Session = Depends(get_db)):
    try:
        supplier = supplier_service.delete_supplier(db, supplier_id)
    except supplier_service.SupplierError as exc:
        return _redirect("/settings", err=str(exc))
    return _redirect("/settings", msg=f"已删除供应商 {supplier.name}")


@app.post("/settings/returns/repair")
def settings_repair_returns(db: Session = Depends(get_db)):
    try:
        result = repair.repair_legacy_returns(db)
    except Exception as exc:
        return _redirect("/settings", err=f"历史归还修复失败：{exc}")
    if result["repaired_lots"]:
        return _redirect(
            "/settings",
            msg=f"已补全 {result['repaired_lots']} 个历史归还批次的批次信息",
        )
    return _redirect("/settings", msg="没有可自动补全的历史归还批次")


@app.get("/settings/backup")
def settings_backup():
    try:
        dest = backup_service.create_backup()
    except backup_service.BackupError as exc:
        return _redirect("/settings", err=str(exc))
    return FileResponse(dest, filename=dest.name, media_type="application/zip")


@app.get("/settings/backup/{name}")
def settings_backup_file(name: str):
    try:
        path = backup_service.resolve_backup(name)
    except backup_service.BackupError as exc:
        return _redirect("/settings", err=str(exc))
    return FileResponse(path, filename=path.name, media_type="application/zip")


@app.post("/settings/restore")
async def settings_restore(
    backup_name: str = Form(""),
    confirm: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    if confirm != "1":
        return _redirect("/settings", err="请先勾选恢复确认")
    temp_path = None
    try:
        if file is not None and file.filename:
            temp_path = BACKUP_DIR / f".restore_upload_{uuid.uuid4().hex}.zip"
            total = 0
            with temp_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > backup_service.MAX_BACKUP_BYTES:
                        raise backup_service.BackupError("备份文件过大（最大 512 MB）")
                    output.write(chunk)
            archive = temp_path
        elif backup_name.strip():
            archive = backup_service.resolve_backup(backup_name.strip())
        else:
            return _redirect("/settings", err="请选择本机备份或上传备份 zip")

        db.close()
        result = backup_service.restore_archive(archive)
        init_db()
        counts = result.get("counts", {})
        summary = "、".join(
            f"{label} {counts[key]}" for key, label in (
                ("parts", "物料"), ("stock_lots", "批次"), ("locations", "库位"),
            ) if key in counts
        )
        msg = "备份已恢复" + (f"：{summary}" if summary else "")
        if result.get("safety_backup"):
            msg += f"；恢复前的数据已保存为 {result['safety_backup']}"
        return _redirect("/settings", msg=msg)
    except backup_service.BackupError as exc:
        return _redirect("/settings", err=str(exc))
    except Exception as exc:
        return _redirect("/settings", err=f"恢复失败：{exc}")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if file is not None:
            await file.close()


def _qr_response(data: str):
    img = labels.qr_image(data, box_size=6)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return Response(content=b.getvalue(), media_type="image/png")


@app.get("/qr.png")
def qr_png_query(data: str = ""):
    return _qr_response(data)


@app.get("/qr/{data}.png")
def qr_png(data: str):
    return _qr_response(data)


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat()}
