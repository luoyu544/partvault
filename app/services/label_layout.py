"""Shared 50x30mm label layout and raster renderer.

The Supvan ``.sef`` template uses 203 dpi (8 px/mm) coordinates and font sizes
down to 9 px.  The official SDK's ``DoPrint`` silently skips any TEXT object
whose font size is below 16 px (2.0 mm), so direct printing cannot use native
text objects and still match the template.  Instead we draw the complete label
once and send it to the SDK as an IMAGE.  The same renderer feeds the A4 PDF,
which keeps direct print, PDF and ``.sef`` visually identical.
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from reportlab.graphics.barcode.code128 import Code128

LABEL_W, LABEL_H = 50.0, 30.0

# Coordinates in millimetres, copied from templates/label_50x30.sef.
BRAND_X, BRAND_Y, BRAND_W, BRAND_H, BRAND_SIZE = 2.0, 1.25, 10.25, 3.0, 1.625
KIND_X, KIND_Y, KIND_W, KIND_H, KIND_SIZE = 12.5, 1.5, 11.25, 2.5, 1.125
PART_X, PART_Y, PART_W, PART_H, PART_SIZE = 1.75, 4.25, 28.75, 7.75, 4.25
SPEC_X, SPEC_Y, SPEC_W, SPEC_H, SPEC_SIZE = 2.0, 12.5, 28.75, 3.5, 1.75
PKG_X, PKG_Y, PKG_W, PKG_H, PKG_SIZE = 2.0, 16.5, 10.0, 3.5, 1.75
QTY_X, QTY_Y, QTY_W, QTY_H, QTY_SIZE = 12.5, 16.5, 6.25, 3.5, 1.75
UNIT_X, UNIT_Y, UNIT_W, UNIT_H, UNIT_SIZE = 18.75, 16.75, 6.25, 3.0, 1.375
LOT_LABEL_X, LOT_LABEL_Y = 2.0, 20.0
LOT_LABEL_W, LOT_LABEL_H, LOT_LABEL_SIZE = 4.5, 2.75, 1.25
LOT_X, LOT_Y, LOT_W, LOT_H, LOT_SIZE = 6.5, 20.0, 25.0, 2.75, 1.25
QR_X, QR_Y, QR_SIZE = 32.25, 5.0, 16.25
BARCODE_X, BARCODE_Y, BARCODE_W, BARCODE_H = 2.0, 23.25, 46.0, 5.25

_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _find_fonts() -> tuple[Path, Path]:
    """Prefer the template's HarmonyOS Sans SC, then Microsoft YaHei.

    On Linux (server deployment) fall back to Noto Sans CJK, then WenQuanYi.
    """
    candidates = [Path(r"C:\Windows\Fonts\HarmonyOS_Sans_SC_Regular.ttf")]
    for base in (Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")):
        candidates.extend(base.glob(
            "Creality/Creality Print*/resources/fonts/HarmonyOS_Sans_SC_Regular.ttf"
        ))
    for regular in candidates:
        bold = regular.with_name("HarmonyOS_Sans_SC_Bold.ttf")
        if regular.is_file() and bold.is_file():
            return regular, bold
    linux_pairs = [
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
         "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
         "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ("/usr/share/fonts/truetype/arphic/uming.ttc",
         "/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    for regular_s, bold_s in linux_pairs:
        regular, bold = Path(regular_s), Path(bold_s)
        if regular.is_file():
            return regular, bold if bold.is_file() else regular
    return _FONT_DIR / "msyh.ttc", _FONT_DIR / "msyhbd.ttc"


_FONT_REGULAR, _FONT_BOLD = _find_fonts()


def _text(x, y, w, h, text, size, bold=False, align=0) -> dict:
    return {
        "x": float(x),
        "y": float(y),
        "w": float(w),
        "h": float(h),
        "text": text or "",
        "size": float(size),
        "bold": bool(bold),
        "align": int(align),
    }


def _fmt_qty(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    return f"{number:g}"


def part_layout(
    part,
    brand: str = "元件库",
    show_price: bool = False,
    kind: str = "元件标签",
    today: str | None = None,
) -> dict:
    """Build draw data for one material label using the .sef geometry."""
    stamp = today or datetime.now().strftime("%Y-%m-%d")
    code = str(getattr(part, "code", "") or "").strip()
    model = getattr(part, "model", "") or getattr(part, "name", "") or code
    name = getattr(part, "name", "") or ""
    package = getattr(part, "package", "") or ""
    unit = getattr(part, "unit", "") or "PCS"
    qty = _fmt_qty(getattr(part, "_qty", 0))

    brand_text, brand_size = fit_line(brand, BRAND_W, BRAND_SIZE, 1.0, bold=True)
    kind_text, kind_size = fit_line(kind, KIND_W, KIND_SIZE, 0.9)
    model_text, model_size = fit_line(model, PART_W, PART_SIZE, 1.75, bold=True)
    name_text, name_size = fit_line(name, SPEC_W, SPEC_SIZE, 1.0)
    package_text, package_size = fit_line(package, PKG_W, PKG_SIZE, 1.0)
    qty_text, qty_size = fit_line(qty, QTY_W, QTY_SIZE, 1.1)
    unit_text, unit_size = fit_line(unit, UNIT_W, UNIT_SIZE, 1.0)

    lot_value = stamp
    price = getattr(part, "_price", None)
    if show_price and price is not None:
        try:
            lot_value += f" · ¥{float(price):g}"
        except (TypeError, ValueError):
            pass
    lot_text, lot_size = fit_line(lot_value, LOT_W, LOT_SIZE, 0.9)

    texts = [
        _text(BRAND_X, BRAND_Y, BRAND_W, BRAND_H, brand_text, brand_size, bold=True),
        _text(KIND_X, KIND_Y, KIND_W, KIND_H, kind_text, kind_size),
        _text(PART_X, PART_Y, PART_W, PART_H, model_text, model_size, bold=True),
        _text(SPEC_X, SPEC_Y, SPEC_W, SPEC_H, name_text, name_size),
        _text(PKG_X, PKG_Y, PKG_W, PKG_H, package_text, package_size),
        _text(QTY_X, QTY_Y, QTY_W, QTY_H, qty_text, qty_size),
        _text(UNIT_X, UNIT_Y, UNIT_W, UNIT_H, unit_text, unit_size),
        _text(LOT_LABEL_X, LOT_LABEL_Y, LOT_LABEL_W, LOT_LABEL_H, "LOT", LOT_LABEL_SIZE),
        _text(LOT_X, LOT_Y, LOT_W, LOT_H, lot_text, lot_size),
    ]
    return {
        "qr": {"data": code, "x": QR_X, "y": QR_Y, "w": QR_SIZE, "h": QR_SIZE},
        "barcode": {
            "data": code,
            "x": BARCODE_X,
            "y": BARCODE_Y,
            "w": BARCODE_W,
            "h": BARCODE_H,
        },
        "texts": texts,
    }


def location_layout(loc, brand: str = "元件库", kind: str = "库位标签") -> dict:
    """Build draw data for one location label using the .sef geometry."""
    code = str(getattr(loc, "code", "") or "").strip()
    name = getattr(loc, "name", "") or ""
    path = getattr(loc, "_path", "") or name
    qr_data = f"PV-L-{code}"

    code_text, code_size = fit_line(code, PART_W, PART_SIZE, 1.75, bold=True)
    name_text, name_size = fit_line(name, SPEC_W, SPEC_SIZE, 1.0)
    path_text, path_size = fit_line(path, SPEC_W, 1.5, 0.95)
    brand_text, brand_size = fit_line(brand, BRAND_W, BRAND_SIZE, 1.0, bold=True)
    kind_text, kind_size = fit_line(kind, 10.0, 1.375, 0.9)

    texts = [
        _text(2.0, 1.25, 10.0, 3.0, kind_text, kind_size),
        _text(37.75, 1.25, BRAND_W, BRAND_H, brand_text, brand_size, bold=True, align=2),
        _text(PART_X, PART_Y, PART_W, PART_H, code_text, code_size, bold=True),
        _text(SPEC_X, SPEC_Y, SPEC_W, SPEC_H, name_text, name_size),
    ]
    # 浅层库位的路径就是编码/名称本身，重复打印没有意义。
    if path_text and path_text not in (code_text, name_text):
        texts.append(_text(SPEC_X, PKG_Y, SPEC_W, PKG_H, path_text, path_size))

    return {
        "qr": {"data": qr_data, "x": QR_X, "y": QR_Y, "w": QR_SIZE, "h": QR_SIZE},
        "barcode": {
            "data": qr_data,
            "x": BARCODE_X,
            "y": BARCODE_Y,
            "w": BARCODE_W,
            "h": BARCODE_H,
        },
        "texts": texts,
    }


@lru_cache(maxsize=64)
def _font(bold: bool, size_px: int):
    path = _FONT_BOLD if bold else _FONT_REGULAR
    if not path.is_file():
        path = _FONT_REGULAR
    if path.is_file():
        try:
            return ImageFont.truetype(str(path), max(1, int(size_px)), index=0)
        except OSError:
            pass
    return ImageFont.load_default()


def _width_em(text: str, bold: bool) -> float:
    """Advance width in em for the actual Windows font used by the renderer."""
    reference = _font(bold, 100)
    try:
        return max(0.01, float(reference.getlength(text)) / 100.0)
    except Exception:
        return max(0.01, units(text))


def fit_line(
    text: str,
    width: float,
    base: float,
    minimum: float,
    bold: bool = False,
) -> tuple[str, float]:
    text = " ".join(str(text or "").split())
    if not text:
        return "", base
    em = _width_em(text, bold)
    size = min(float(base), float(width) / em)
    size = max(float(minimum), round(size - 0.005, 2))
    if em * size <= width:
        return text, size

    # Even the minimum size is too wide: keep as many characters as fit and
    # add an ellipsis, measured with the same font metrics.
    ellipsis = "…"
    clipped = text
    while clipped and _width_em(clipped + ellipsis, bold) * size > width:
        clipped = clipped[:-1]
    return (clipped.rstrip() + ellipsis) if clipped else ellipsis, size


def _draw_qr(image: Image.Image, data: str, box: dict, scale: int) -> None:
    if not data:
        return
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=1,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    module = Image.new("L", (size, size), 255)
    pixels = module.load()
    for y, row in enumerate(matrix):
        for x, value in enumerate(row):
            if value:
                pixels[x, y] = 0
    width = max(1, int(round(float(box["w"]) * scale)))
    height = max(1, int(round(float(box["h"]) * scale)))
    qr_image = module.resize((width, height), Image.Resampling.NEAREST)
    image.paste(qr_image, (int(round(float(box["x"]) * scale)), int(round(float(box["y"]) * scale))))


def _code128_bars(data: str) -> tuple[list[tuple[int, int]], int]:
    barcode = Code128(data)
    barcode.validate()
    barcode.encode()
    pattern = barcode.decompose()
    bars: list[tuple[int, int]] = []
    left = 0
    for ch in pattern:
        if "a" <= ch <= "z":
            left += ord(ch) - ord("a") + 1
        elif "A" <= ch <= "Z":
            width = ord(ch) - ord("A") + 1
            bars.append((left, width))
            left += width
    return bars, left


def _draw_barcode(draw: ImageDraw.ImageDraw, data: str, box: dict, scale: int) -> None:
    if not data:
        return
    try:
        bars, total = _code128_bars(data)
    except Exception:
        return
    if not bars or total <= 0:
        return

    x = int(round(float(box["x"]) * scale))
    y = int(round(float(box["y"]) * scale))
    box_w = int(round(float(box["w"]) * scale))
    box_h = int(round(float(box["h"]) * scale))
    # A 0.75mm quiet zone on both sides matches the .sef barcode bitmap.
    quiet = max(1, int(round(0.75 * scale)))
    usable = max(1, box_w - quiet * 2)
    bar_unit = usable / total
    for left, width in bars:
        x0 = x + quiet + int(round(left * bar_unit))
        x1 = x + quiet + int(round((left + width) * bar_unit))
        if x1 <= x0:
            x1 = x0 + 1
        draw.rectangle((x0, y, x1 - 1, y + box_h - 1), fill=0)


def _render_image(layout: dict, scale: float) -> Image.Image:
    """Draw a layout at ``scale`` pixels per millimetre (grayscale, antialiased)."""
    width = int(round(LABEL_W * scale))
    height = int(round(LABEL_H * scale))
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)

    qr = layout.get("qr") or {}
    _draw_qr(image, str(qr.get("data") or ""), qr, scale)
    barcode = layout.get("barcode") or {}
    _draw_barcode(draw, str(barcode.get("data") or ""), barcode, scale)

    for item in layout.get("texts") or []:
        text = str(item.get("text") or "")
        if not text:
            continue
        size_px = max(1, int(round(float(item.get("size") or 1.0) * scale)))
        font = _font(bool(item.get("bold")), size_px)
        align = int(item.get("align") or 0)
        x = float(item.get("x") or 0.0) * scale
        y = float(item.get("y") or 0.0) * scale
        w = float(item.get("w") or 0.0) * scale
        h = float(item.get("h") or 0.0) * scale
        if align == 1:
            position = (x + w / 2.0, y + h / 2.0)
            anchor = "mm"
        elif align == 2:
            position = (x + w, y + h / 2.0)
            anchor = "rm"
        else:
            position = (x, y + h / 2.0)
            anchor = "lm"
        draw.text(position, text, font=font, fill=0, anchor=anchor)

    return image


def render_png(layout: dict, scale: float = 8) -> bytes:
    """Render a layout to a grayscale PNG at ``scale`` pixels per millimetre."""
    image = _render_image(layout, scale)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
