import copy
import io
import json
from datetime import datetime

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from ..db import BASE_DIR
from .label_layout import location_layout, part_layout, render_png

TEMPLATE_SEF = BASE_DIR / "templates" / "label_50x30.sef"


def qr_image(data: str, box_size: int = 8):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size, border=1)
    qr.add_data(data or "-")
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def _fmt_qty(q) -> str:
    try:
        f = float(q or 0)
    except Exception:
        return str(q)
    return f"{f:g}"


def _pdf_label_image(layout: dict, scale: int = 16):
    from reportlab.lib.utils import ImageReader

    return ImageReader(io.BytesIO(render_png(layout, scale)))


def _draw_label(c, layout: dict, x: float, y: float) -> None:
    c.drawImage(_pdf_label_image(layout), x, y,
                width=50 * mm, height=30 * mm)


def build_pdf(parts, brand: str = "元件库", show_price: bool = False,
              kind: str = "元件标签") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    label_w, label_h = 50 * mm, 30 * mm
    gap_x, gap_y = 3 * mm, 2 * mm
    cols, rows = 3, 8
    total_w = cols * label_w + (cols - 1) * gap_x
    total_h = rows * label_h + (rows - 1) * gap_y
    left = (page_w - total_w) / 2
    bottom0 = (page_h - total_h) / 2

    for idx, part in enumerate(parts):
        pos = idx % (cols * rows)
        if idx and pos == 0:
            c.showPage()
        col = pos % cols
        row = pos // cols
        x = left + col * (label_w + gap_x)
        y = bottom0 + total_h - label_h - row * (label_h + gap_y)
        _draw_label(c, part_layout(part, brand=brand, show_price=show_price,
                                   kind=kind), x, y)

    c.save()
    return buf.getvalue()



def _fit_font(text: str, base: int, comfortable: int = 10, minimum: int = 12) -> int:
    n = len(text or "")
    if n <= comfortable:
        return base
    size = int(base * (comfortable / n) ** 0.65)
    return max(minimum, min(base, size))


def _set_text(
    item: dict,
    text: str,
    font_size: int | None = None,
    x: int | None = None,
    y: int | None = None,
    h_align: int | None = None,
) -> None:
    text = text or ""
    # Supvan 的 DrawText.Text 使用 \x00 作为文本结束符；缺少它时编辑器会显示空白。
    item["Text"] = (text + "\x00") if text else ""
    item["IsEmpty"] = not bool(text.strip())
    # 这些字段原来指向 Excel 列。导出为固定内容时必须同时清掉绑定信息，
    # 否则在没有 DataBaseMessage 的情况下编辑器可能按空数据绘制。
    item["DataColumn"] = ""
    item["DataColumnName"] = ""
    item["DataColumnFormat"] = ""
    item["IsBdExcel"] = False
    item["isbdexcel"] = False
    if font_size is not None:
        fs = int(font_size)
        item["FontSize"] = fs
        for key in ("FontFormat", "CurrentFontFormat"):
            if isinstance(item.get(key), dict):
                item[key]["FontSize"] = fs
    if h_align is not None:
        for key in ("FontFormat", "CurrentFontFormat"):
            if isinstance(item.get(key), dict):
                item[key]["HorizontalAlignment"] = int(h_align)
    if x is not None:
        item["X"] = int(x)
        b = item.get("bounds")
        if isinstance(b, dict):
            width = b.get("Width", 90)
            b["X"] = int(x)
            b["Left"] = int(x)
            b["Right"] = int(x) + int(width)
            if isinstance(b.get("Location"), dict):
                b["Location"]["X"] = int(x)
        nb = item.get("NoRotationBounds")
        if isinstance(nb, str):
            try:
                parts = [int(float(v.strip())) for v in nb.split(",")]
                if len(parts) == 4:
                    parts[0] = int(x)
                    if isinstance(b, dict):
                        parts[2] = int(b.get("Width", parts[2]))
                    item["NoRotationBounds"] = ", ".join(str(v) for v in parts)
            except Exception:
                pass
    if y is not None:
        item["Y"] = int(y)
        b = item.get("bounds")
        if isinstance(b, dict):
            height = b.get("Height", 20)
            b["Y"] = int(y)
            b["Top"] = int(y)
            b["Bottom"] = int(y) + int(height)
            if isinstance(b.get("Location"), dict):
                b["Location"]["Y"] = int(y)
        nb = item.get("NoRotationBounds")
        if isinstance(nb, str):
            try:
                parts = [int(float(v.strip())) for v in nb.split(",")]
                if len(parts) == 4:
                    parts[1] = int(y)
                    item["NoRotationBounds"] = ", ".join(str(v) for v in parts)
            except Exception:
                pass


def _apply_paper_settings(data: dict) -> None:
    """把导出文件统一设为 50x30 间隙纸：上下边距 1.5mm，标签间隙 2mm。"""
    data["LabelKindName"] = "间隙标签"

    def apply_model(container) -> None:
        if not isinstance(container, dict):
            return
        raw = container.get("ModelJson")
        if not raw:
            return
        try:
            model = json.loads(raw)
        except Exception:
            return
        model["PaperType"] = 1  # Supvan PaperType.DieCut = 间隙纸
        padding = model.get("Padding")
        if not isinstance(padding, dict):
            padding = {}
        padding["Top"] = 1.5
        padding["Bottom"] = 1.5
        model["Padding"] = padding
        model["DieCutGap"] = 2.0
        container["ModelJson"] = json.dumps(model, ensure_ascii=False)

    apply_model(data.get("PageModel"))
    for page in data.get("Pages") or []:
        apply_model(page.get("PageModel"))


def build_sef(parts, brand: str = "元件库", kind: str = "元件标签") -> bytes:
    data = json.loads(TEMPLATE_SEF.read_text(encoding="utf-8-sig"))
    base_page = data["Pages"][0]
    base_page["Items"] = base_page.get("Items", [])
    pages = []

    for idx, part in enumerate(parts):
        page = copy.deepcopy(base_page)
        page["Name"] = f"标签页{idx + 1}"
        page["RowIndex"] = idx
        page["ColumnIndex"] = None
        page["PageOrderIndex"] = idx
        items = page.get("Items", [])
        by_name = {}
        for it in items:
            try:
                j = json.loads(it["Json"])
            except Exception:
                continue
            nm = j.get("name")
            by_name[nm] = j
            j["isbdexcel"] = False
            j["IsBdExcel"] = False
            it["Json"] = json.dumps(j, ensure_ascii=False)

        model_text = (part.model or part.name or part.code or "")[:60]
        name_text = (part.name or "")[:80]
        brand_text = (brand or "")[:20]
        kind_text = (kind or "元件标签")[:10]
        pkg_text = (part.package or "")[:20]
        lot_text = datetime.now().strftime("%Y-%m-%d")

        updates = {
            "brand": (brand_text, 13),
            "kind": (kind_text, 9),
            "part": (model_text, 34),
            "spec": (name_text, 14),
            "pkg": (pkg_text, 14),
            "qty": (_fmt_qty(getattr(part, "_qty", 0)), 14),
            "unit": (part.unit or "PCS", 11),
            "lot": (lot_text, 10),
        }
        for nm, (text, base) in updates.items():
            j = by_name.get(nm)
            if not j:
                continue
            if nm == "part":
                fs = _fit_font(text, base, comfortable=9, minimum=16)
            elif nm == "brand":
                fs = _fit_font(text, base, comfortable=5, minimum=8)
            elif nm == "spec":
                fs = _fit_font(text, base, comfortable=18, minimum=9)
            elif nm == "pkg":
                fs = _fit_font(text, base, comfortable=8, minimum=9)
            else:
                fs = base
            _set_text(j, text, fs)
        if "qty" in by_name:
            _set_text(by_name["qty"], updates["qty"][0], 14, x=100)
        if "unit" in by_name:
            _set_text(by_name["unit"], updates["unit"][0], 11, x=150)
        if "kind" in by_name:
            _set_text(by_name["kind"], kind_text, 9, x=100)

        code = part.code or ""
        for nm in ("qr", "bc"):
            j = by_name.get(nm)
            if j:
                j["Content"] = code
                j["OldContent"] = code
                j["DataColumn"] = ""
                j["DataColumnName"] = ""
                j["DataColumnFormat"] = ""
                j["isbdexcel"] = False
                j["IsBdExcel"] = False
        for it in items:
            try:
                j = json.loads(it["Json"])
                nm = j.get("name")
                final = by_name.get(nm, j)
                final["isbdexcel"] = False
                final["IsBdExcel"] = False
                it["Json"] = json.dumps(final, ensure_ascii=False)
            except Exception:
                pass
        pages.append(page)

    data["Pages"] = pages
    data["PageOrderCounter"] = len(pages)
    data["OpenedPageIndex"] = 0
    data.pop("DataBaseMessage", None)
    _apply_paper_settings(data)
    return json.dumps(data, ensure_ascii=False).encode("utf-8-sig")

def location_qr_data(loc) -> str:
    return f"PV-L-{loc.code or ''}"


def build_location_pdf(locations, brand: str = "元件库",
                       kind: str = "库位标签") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4

    label_w, label_h = 50 * mm, 30 * mm
    gap_x, gap_y = 3 * mm, 2 * mm
    cols, rows = 3, 8
    total_w = cols * label_w + (cols - 1) * gap_x
    total_h = rows * label_h + (rows - 1) * gap_y
    left = (page_w - total_w) / 2
    bottom0 = (page_h - total_h) / 2

    for idx, loc in enumerate(locations):
        pos = idx % (cols * rows)
        if idx and pos == 0:
            c.showPage()
        col = pos % cols
        row = pos // cols
        x = left + col * (label_w + gap_x)
        y = bottom0 + total_h - label_h - row * (label_h + gap_y)
        _draw_label(c, location_layout(loc, brand=brand, kind=kind), x, y)

    c.save()
    return buf.getvalue()



def build_location_sef(locations, brand: str = "元件库", kind: str = "库位标签") -> bytes:
    data = json.loads(TEMPLATE_SEF.read_text(encoding="utf-8-sig"))
    base_page = data["Pages"][0]
    base_page["Items"] = base_page.get("Items", [])
    pages = []

    for idx, loc in enumerate(locations):
        page = copy.deepcopy(base_page)
        page["Name"] = f"库位标签{idx + 1}"
        page["RowIndex"] = idx
        page["ColumnIndex"] = None
        page["PageOrderIndex"] = idx
        items = page.get("Items", [])
        by_name = {}
        for it in items:
            try:
                j = json.loads(it["Json"])
            except Exception:
                continue
            nm = j.get("name")
            by_name[nm] = j
            j["isbdexcel"] = False
            j["IsBdExcel"] = False
            it["Json"] = json.dumps(j, ensure_ascii=False)

        # 库位签：左上角“库位标签”，右上角品牌；不显示数量/LOT，
        # 但保留底部条形码，和二维码一样写入库位码载荷。
        code = (loc.code or "")[:60]
        name_text = (loc.name or "")[:80]
        path_text = (getattr(loc, "_path", "") or loc.name or "")[:80]
        kind_text = (kind or "库位标签")[:10]
        brand_text = (brand or "")[:20]

        updates = {
            "part": (code, 34),
            "spec": (name_text, 14),
            "pkg": (path_text if path_text not in (code, name_text) else "", 12),
        }
        for nm, (text, base) in updates.items():
            j = by_name.get(nm)
            if not j:
                continue
            if nm == "part":
                fs = _fit_font(text, base, comfortable=9, minimum=14)
            elif nm == "spec":
                fs = _fit_font(text, base, comfortable=14, minimum=9)
            elif nm == "pkg":
                fs = _fit_font(text, base, comfortable=18, minimum=8)
            else:
                fs = base
            _set_text(j, text, fs)
        for nm in ("qty", "unit", "lotlabel", "lot"):
            if nm in by_name:
                _set_text(by_name[nm], "")
        if "kind" in by_name:
            # 副标题放到左上角，作为库位签的标识。
            _set_text(by_name["kind"], kind_text, 11, x=16, y=10)
        if "brand" in by_name:
            # 品牌放右上角，和直接打印/A4 PDF 的库位签一致。
            _set_text(by_name["brand"], brand_text, 13, x=294, y=10, h_align=2)

        qr_data = location_qr_data(loc)
        for nm in ("qr", "bc"):
            j = by_name.get(nm)
            if j:
                j["Content"] = qr_data
                j["OldContent"] = qr_data
                j["DataColumn"] = ""
                j["DataColumnName"] = ""
                j["DataColumnFormat"] = ""
                j["isbdexcel"] = False
                j["IsBdExcel"] = False
        for it in items:
            try:
                j = json.loads(it["Json"])
                nm = j.get("name")
                final = by_name.get(nm, j)
                final["isbdexcel"] = False
                final["IsBdExcel"] = False
                it["Json"] = json.dumps(final, ensure_ascii=False)
            except Exception:
                pass
        pages.append(page)

    data["Pages"] = pages
    data["PageOrderCounter"] = len(pages)
    data["OpenedPageIndex"] = 0
    data.pop("DataBaseMessage", None)
    _apply_paper_settings(data)
    return json.dumps(data, ensure_ascii=False).encode("utf-8-sig")
