import io
import re
from pathlib import Path

import zxingcpp
from PIL import Image, ImageOps

_engine = None

# 封装/尺寸词典：既包含标准封装，也包含连接器间距、模块尺寸。
PACKAGES = [
    r"0201", r"0402", r"0603", r"0805", r"1206", r"1210", r"1812", r"2010", r"2512",
    r"SOT-?23(?:-\d+)?", r"TSOT-?23(?:-\d+)?", r"SOD-?123", r"SOD-?323",
    r"TSSOP-?\d+", r"SSOP-?\d+", r"MSOP-?\d+", r"SOIC-?\d+", r"SOP-?\d+",
    r"QFN-?\d+", r"DFN-?\d+", r"LQFP-?\d+", r"TQFP-?\d+", r"BGA-?\d+",
    r"TO-?220", r"TO-?92", r"TO-?252", r"TO-?263", r"DO-?214", r"DO-?41", r"DO-?35",
    r"SMA", r"SMB", r"SMC", r"DIP-?\d+", r"PLCC-?\d+", r"HC-?49", r"3225", r"5032",
    r"1040", r"1265", r"1770", r"0850", r"6060", r"OP-?\d+",
]
PACKAGE_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(PACKAGES) + r")(?![A-Za-z0-9])", re.IGNORECASE)
PIN_RE = re.compile(r"(?<![A-Za-z0-9])(\d+x\d+P)", re.IGNORECASE)
PITCH_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
SIZE3_RE = re.compile(r"(?<![\d.])(\d+)\*(\d+)\*(\d+)")


def _find_package_matches(text: str) -> list[str]:
    out: list[str] = []
    for m in PACKAGE_RE.finditer(text):
        out.append(m.group(1))
    for m in PIN_RE.finditer(text):
        out.append(m.group(1))
    for m in PITCH_RE.finditer(text):
        out.append(m.group(0).strip())
    for m in SIZE3_RE.finditer(text):
        try:
            nums = [int(m.group(i)) for i in (1, 2, 3)]
        except Exception:
            continue
        if max(nums) <= 40:
            out.append(m.group(0))
    return out


def _is_package_token(token: str) -> bool:
    return bool(
        PACKAGE_RE.fullmatch(token)
        or PIN_RE.fullmatch(token)
        or PITCH_RE.fullmatch(token)
        or SIZE3_RE.fullmatch(token)
    )

BRANDS = [
    "TI", "ST", "STM", "Microchip", "Atmel", "Espressif", "乐鑫", "兆易", "GigaDevice",
    "MPS", "芯源", "南京微盟", "MICRONE", "Yageo", "国巨", "Samsung", "三星", "Murata",
    "村田", "TDK", "风华", "厚声", "Vishay", "ON", "NXP", "Infineon", "英飞凌",
    "Renesas", "ROHM", "Toshiba", "东芝", "Panasonic", "松下", "Kemet", "AVX",
    "Nichicon", "Rubycon", "Wurth", "伍尔特", "Molex", "JST", "TE", "Amphenol",
    "立创", "LCSC", "莱联", "LAIL", "意法半导体", "STMicroelectronics",
]
BRAND_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(b) for b in sorted(BRANDS, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)

NAME_WORDS = [
    "电阻", "电容", "电感", "二极管", "三极管", "MOS", "芯片", "集成电路", "连接器",
    "端子", "晶振", "传感器", "模块", "开发板", "继电器", "保险丝", "螺丝", "螺母",
    "铜柱", "排针", "排母", "开关", "按键", "散热", "风扇", "电源", "电池", "工具",
    "焊锡", "胶带", "插座", "插头", "线材", "转接", "单片机", "微控制器", "MCU",
    "驱动", "接口", "稳压", "运放", "光耦",
]

MODEL_LABEL_RE = re.compile(
    r"(?:型号|规格型号|商品型号|产品型号|Model|MODEL|model)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-]{2,40})"
)
MERCHANT_CODE_RE = re.compile(
    r"(?:商家编码|商品编码|商家编号|货号|SKU|sku)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/\-]{2,40})"
)
CCODE_RE = re.compile(r"(?<![A-Za-z0-9])C\d{4,}")
PVCODE_RE = re.compile(r"(?<![A-Za-z0-9])PV-[PLB]-[A-Za-z0-9\-]+(?![A-Za-z0-9])")
LABEL_CODE_RE = re.compile(r"^\d{4}-[A-Z]-\w{2,}-\d{3}$", re.IGNORECASE)
PREFERRED_MODEL_RE = re.compile(
    r"^(?:STM32|STM8|ESP|GD32|ATmega|ATtiny|PIC|TPS|LM|AMS|NE|SS|1N|2N|BC|AO|IRF|BTS|XL|CH|CP|MAX|AD|DAC|ADS|MCP|W25|MT|ISD|HT|SN|UCC|TL|LDO|RT|SY|MP|FP|IP|SI|RTL|BK|NRF|HC)[A-Z0-9.\-]{2,}$",
    re.IGNORECASE,
)
MODEL_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*")
SIZE_LIST_RE = re.compile(r"^(?:\d+)?(?:M\d+){2,}$", re.IGNORECASE)
RANGE_MODEL_RE = re.compile(r"^\d+(?:[-/]\d+)+[A-Z]?$", re.IGNORECASE)
RATING_RE = re.compile(
    r"^(?:(?:AC|DC)?\d+(?:\.\d+)?(?:V|MV|A|MA|W|MW|HZ|KHZ|MHZ|GHZ|OHM|K|R|F|UF|NF|PF|MAH|AH|MM|CM|M|MN|UM|NM)|\d+(?:\.\d+)?(?:A|V|W)(?:MG|G|M)?)$",
    re.IGNORECASE,
)

QTY_LABEL_RE = re.compile(r"(?:QTY|Qty|qty|数量)\s*[:：]?\s*(\d+(?:\.\d+)?)")
UNIT_QTY_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(个|只|片|条|包|盒|卷|套|组|对|支|根|袋|箱|张|pcs)(?![\dA-Za-z码款形纹])",
    re.IGNORECASE,
)
EXPLICIT_SPEC_RES = [
    re.compile(r"(?:拍|买|每|一)\s*(?:件|个|份|包|盒|卷)?\s*[=:：]\s*(\d+(?:\.\d+)?)\s*(?:个|只|片|条|包|盒|卷|套|组|对|支|根|袋|箱|张)", re.IGNORECASE),
    re.compile(r"(?:1|一)\s*(?:件|个|份|包|盒|卷)\s*[=:：]\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
]
UNIT_PRIORITY = {
    "卷": 5, "包": 5, "盒": 5, "袋": 5, "箱": 5,
    "套": 4, "组": 4, "对": 4, "件": 4, "条": 4, "根": 4, "支": 4,
    "个": 3, "只": 3, "片": 3, "pcs": 3,
    "张": 1,
}
X_LINE_RE = re.compile(r"^\s*[xX×]\s*(\d{1,4})(?:\s*(?:件|个|台|套|只))?\s*$")
PURCHASE_QTY_RE = re.compile(r"(?:购买数量|数量)\s*[:：]?\s*(\d{1,4})")

DATE_RE = re.compile(r"\b(20\d{2})[-/.]?(0[1-9]|1[0-2])[-/.]?(0[1-9]|[12]\d|3[01])\b")
LOT_RE = re.compile(r"(?:LOT|Lot|lot|批次|批号|DC|D/C|日期码)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-/.]{3,})")
PRICE_RE = [
    re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)"),
    re.compile(r"(\d+(?:\.\d+)?)\s*元"),
]
SKIP_TOKENS = {
    "PCS", "SMD", "SMT", "ROHS", "NPN", "PNP", "IC", "LED", "USB", "MCU", "DC", "AC",
    "LOT", "QTY", "MODEL", "PN", "SN", "GND", "VCC", "DATA", "CLK", "RESET",
}
ACTION_WORDS = (
    "退款", "退货", "加入购物车", "交易快照", "极速", "7天", "包邮", "实付",
    "优惠", "券", "售后", "价保", "假一赔", "采购", "无忧", "官方", "旗舰",
    "发货", "运费", "客服", "收藏", "对比", "申请", "退换", "买后必看", "去看看",
)
SKU_LIKE_RE = re.compile(
    r"(?:\d+x\d+P|[（(]\s*\d+\s*(?:个|只|片|条|包|盒|卷|套|组|对|支|根|袋|箱|张)|[;；].*\d+\s*(?:个|只|片|条|包|盒|卷|套|组|对|支|根|袋|箱|张))",
    re.IGNORECASE,
)


def get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


def decode_codes(image_path: str) -> list[str]:
    codes: list[str] = []
    try:
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        for r in zxingcpp.read_barcodes(img):
            t = (r.text or "").strip()
            if t and t not in codes:
                codes.append(t)
    except Exception:
        pass
    return codes


def decode_codes_bytes(data: bytes) -> list[str]:
    """识别手机拍照上传的二维码/条码，返回去重后的内容。"""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((2400, 2400))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        codes: list[str] = []
        for r in zxingcpp.read_barcodes(img):
            t = (r.text or "").strip()
            if t and t not in codes:
                codes.append(t)
        return codes
    except Exception:
        return []


def _load_image(image_path: str) -> Image.Image:
    """统一做 EXIF 方向、尺寸限制和小图放大，避免截图文字过小。"""
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > 2400:
        img.thumbnail((2400, 2400))
    if img.width < 900:
        scale = min(2.0, 900.0 / max(1, img.width))
        if scale > 1.05:
            img = img.resize(
                (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
    return img


def _parse_result(result) -> list[dict]:
    blocks: list[dict] = []
    for item in result or []:
        if len(item) < 2:
            continue
        text = (item[1] or "").strip().strip("\x00").strip()
        if not text:
            continue
        box = item[0]
        pts = []
        try:
            for p in box:
                pts.append((int(round(float(p[0]))), int(round(float(p[1])))))
        except Exception:
            pts = []
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
        else:
            x0 = y0 = x1 = y1 = 0
        try:
            conf = float(item[2]) if len(item) > 2 and item[2] is not None else 0.7
        except Exception:
            conf = 0.7
        blocks.append({
            "text": text,
            "confidence": round(max(0.0, min(1.0, conf)), 4),
            "box": [list(p) for p in pts],
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
            "width": max(0, x1 - x0),
            "height": max(1, y1 - y0),
        })
    return blocks


def _decode_codes_image(image: Image.Image) -> list[str]:
    codes: list[str] = []
    try:
        for r in zxingcpp.read_barcodes(image):
            t = (r.text or "").strip()
            if t and t not in codes:
                codes.append(t)
    except Exception:
        pass
    return codes


def _recognize_image(image: Image.Image) -> dict:
    # 直接传 PIL.Image，RapidOCR 会自行把 RGB 转成 BGR；传 ndarray 会被当成 BGR。
    result, _ = get_engine()(image)
    blocks = _parse_result(result)
    lines = [b["text"] for b in blocks]
    codes = _decode_codes_image(image)
    analysis = _analyze(lines, codes, blocks)
    return {
        "lines": lines,
        "raw_text": "\n".join(lines),
        "codes": codes,
        "blocks": blocks,
        "source": analysis["source"],
        "confidence": analysis["confidence"],
        "evidence": analysis["evidence"],
        "qty_detail": analysis["qty_detail"],
        "fields": analysis["fields"],
    }


def _ocr_quality(result: dict) -> float:
    blocks = result.get("blocks", []) or []
    good = [b for b in blocks if len(str(b.get("text", ""))) >= 3]
    avg = sum(float(b.get("confidence") or 0) for b in good) / len(good) if good else 0.0
    fields = result.get("fields", {}) or {}
    score = len(good) * 0.12 + avg * 2.0
    if fields.get("supplier_part_no"):
        score += 1.5
    if fields.get("model"):
        score += 1.5
    if fields.get("package"):
        score += 0.4
    if result.get("codes"):
        score += 1.0
    if result.get("source") == "label":
        score += 0.2
    return score


def _needs_orientation_retry(result: dict) -> bool:
    if result.get("source") != "label":
        return False
    fields = result.get("fields", {}) or {}
    if not fields.get("supplier_part_no") or not fields.get("model") or not fields.get("package"):
        return True
    return not any("QTY" in str(b.get("text", "")).upper() for b in result.get("blocks", []) or [])


def recognize(image_path: str) -> dict:
    image_path = str(image_path)
    image = _load_image(image_path)
    best = _recognize_image(image)
    # 标签照片可能横放/倒放。只在标签字段明显缺失时尝试 90/180/270°，避免正常图片变慢。
    if _needs_orientation_retry(best):
        best_score = _ocr_quality(best)
        for angle in (90, 180, 270):
            rotated = image.rotate(angle, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
            candidate = _recognize_image(rotated)
            score = _ocr_quality(candidate)
            if score > best_score + 0.25:
                best = candidate
                best_score = score
    return best


def extract_fields(lines: list[str], codes: list[str], blocks: list[dict] | None = None) -> dict:
    """兼容旧调用：只返回 fields。"""
    return _analyze(lines, codes, blocks or [])["fields"]


def _clean_text(text: str) -> str:
    text = str(text or "").replace("\x00", "").strip()
    text = re.sub(r"\[?\s*交易快照\s*\]?", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_action_text(text: str) -> bool:
    if not text:
        return True
    if "￥" in text or "¥" in text:
        return True
    return any(w in text for w in ACTION_WORDS)


def _is_sku_like(text: str) -> bool:
    return bool(SKU_LIKE_RE.search(text or ""))


def _is_noise(text: str) -> bool:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    digits = len(re.findall(r"\d", text))
    if re.fullmatch(r"\d+[A-Za-z]{1,3}", text):
        return False
    if re.fullmatch(r"[A-Za-z]{1,2}", text):
        return True
    if digits >= 2 and chinese <= 1:
        return True
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return True
    return False


def _norm_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _is_unit_token(token: str) -> bool:
    u = token.upper()
    if u in {"V", "MV", "KV", "A", "MA", "W", "MW", "MM", "CM", "M", "KM", "G", "KG",
             "HZ", "KHZ", "MHZ", "GHZ", "F", "UF", "NF", "PF", "OHM", "K", "R", "P",
             "MAH", "AH", "WH", "KWH", "C", "S", "MS", "US", "NS", "BIT", "KB", "MB",
             "GB", "TB", "L", "ML", "BAR", "MPA", "N", "NM", "LB", "OZ", "PCS", "PC"}:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?(?:V|MV|KV|A|MA|W|MW|MM|CM|M|KM|G|KG|HZ|KHZ|MHZ|GHZ|F|UF|NF|PF|OHM|K|R|P|MAH|AH|WH|KWH|C|S|MS|US|NS|BIT|KB|MB|GB|TB|L|ML|BAR|MPA|N|NM|LB|OZ)", u):
        return True
    if re.fullmatch(r"M\d+(?:\.\d+)?", u):
        return True
    return False


def _line_conf(blocks: list[dict], index: int | None, default: float = 0.7) -> float:
    if index is not None and 0 <= index < len(blocks):
        return float(blocks[index].get("confidence") or default)
    return default


def _find_line(lines: list[str], text: str) -> int | None:
    if not text:
        return None
    for i, line in enumerate(lines):
        if text in line:
            return i
    return None


def _parse_barcode_payload(codes: list[str]) -> dict:
    out = {"part_no": "", "model": "", "qty": None}
    for code in codes or []:
        s = str(code or "")
        m = re.search(r"(?:^|[,{])pc:([^,}]+)", s, re.IGNORECASE)
        if m and m.group(1).strip().lower() not in ("", "null", "none"):
            out["part_no"] = m.group(1).strip()
        m = re.search(r"(?:^|[,{])pm:([^,}]+)", s, re.IGNORECASE)
        if m and m.group(1).strip().lower() not in ("", "null", "none"):
            out["model"] = m.group(1).strip()
        m = re.search(r"(?:^|[,{])qty:(\d+(?:\.\d+)?)", s, re.IGNORECASE)
        if m:
            try:
                out["qty"] = float(m.group(1))
            except Exception:
                pass
    return out


def _detect_source(lines: list[str], codes: list[str]) -> str:
    flat = " ".join(lines)
    has_price = bool(re.search(r"[¥￥]", flat)) or bool(re.search(r"\d+(?:\.\d+)?\s*元", flat))
    has_qty_label = bool(re.search(r"(?:QTY|Qty|qty)\s*[:：]", flat))
    has_label_payload = any("pc:" in str(c).lower() and "pm:" in str(c).lower() for c in codes or [])
    has_x = any(X_LINE_RE.fullmatch(str(line).strip()) for line in lines)
    if has_qty_label and not has_price:
        return "label"
    if has_label_payload and not has_price:
        return "label"
    if has_price or "交易快照" in flat or "实付" in flat:
        return "purchase"
    if has_qty_label:
        return "label"
    if has_x:
        return "purchase"
    return "unknown"


def _extract_label_qty(lines: list[str]) -> tuple[float | None, int | None, str]:
    for i, line in enumerate(lines):
        m = QTY_LABEL_RE.search(line)
        if m and "QTY" in line.upper():
            try:
                return float(m.group(1)), i, line
            except Exception:
                pass
    return None, None, ""


def _extract_purchase_qty(lines: list[str]) -> tuple[float | None, int | None, str]:
    for i, line in enumerate(lines):
        m = X_LINE_RE.fullmatch(str(line).strip())
        if m:
            try:
                return float(m.group(1)), i, line.strip()
            except Exception:
                pass
    for i, line in enumerate(lines):
        if "QTY" in line.upper():
            continue
        m = PURCHASE_QTY_RE.search(line)
        if m:
            try:
                return float(m.group(1)), i, line.strip()
            except Exception:
                pass
    return None, None, ""


def _extract_spec_qty(lines: list[str]) -> tuple[float | None, int | None, str, bool]:
    # “拍一件=10个”“1件=10个”优先，避免把 1 件当成规格数量。
    for i, line in enumerate(lines):
        text = _clean_text(line)
        if _is_action_text(text) or "￥" in text or "¥" in text:
            continue
        for p in EXPLICIT_SPEC_RES:
            m = p.search(text)
            if m:
                try:
                    return float(m.group(1)), i, text, False
                except Exception:
                    pass

    candidates: list[tuple[int, int, float, str, str]] = []
    for i, line in enumerate(lines):
        text = _clean_text(line)
        if not text or _is_action_text(text) or "￥" in text or "¥" in text:
            continue
        if "QTY" in text.upper():
            continue
        for m in UNIT_QTY_RE.finditer(text):
            try:
                num = float(m.group(1))
            except Exception:
                continue
            if num <= 0 or num > 100000:
                continue
            unit = m.group(2).lower()
            priority = UNIT_PRIORITY.get(unit, 1)
            candidates.append((priority, i, num, unit, text))

    if not candidates:
        return None, None, "", False
    top_priority = max(c[0] for c in candidates)
    top = [c for c in candidates if c[0] == top_priority]
    ambiguous = len({c[2] for c in top}) > 1
    chosen = sorted(top, key=lambda c: (c[1], -c[2]))[0]
    return chosen[2], chosen[1], chosen[4], ambiguous


def _merge_title(blocks: list[dict], lines: list[str]) -> tuple[str, float, str]:
    if not blocks:
        return _fallback_name(lines), 0.55, ""

    items = []
    for i, b in enumerate(blocks):
        text = _clean_text(b.get("text", ""))
        if not text:
            continue
        if _is_action_text(text):
            continue
        if re.match(r"^\s*QTY", text, re.IGNORECASE):
            continue
        items.append((i, b, text))
    if not items:
        return _fallback_name(lines), 0.5, ""

    def score(item):
        _, _, text = item
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        score = chinese * 3 + len(text)
        lower = text.lower()
        if any(w.lower() in lower for w in NAME_WORDS):
            score += 10
        if any(w in text for w in ("系列", "超值", "主流", "原装", "正品", "优质", "全新", "厂家", "直销", "热卖")):
            score -= 10
        if any(w in text for w in ("买后必看", "检查", "必看")):
            score -= 20
        return score

    best = max(items, key=score)
    best_i, best_b, best_text = best
    chosen = [best]
    last = best_b
    last_text = best_text
    avg_conf = float(best_b.get("confidence") or 0.7)

    for item in sorted(items, key=lambda x: (x[1]["y0"], x[1]["x0"])):
        i, b, text = item
        if i == best_i or b["y0"] <= best_b["y0"]:
            continue
        if b["y0"] < last["y0"]:
            continue
        gap = b["y0"] - last["y1"]
        if gap > max(18, 2.4 * last["height"]):
            break
        overlap = min(last["x1"], b["x1"]) - max(last["x0"], b["x0"])
        if overlap < 0.45 * max(1, min(last["width"], b["width"])):
            continue
        if _is_sku_like(text) or _is_noise(text):
            if _is_sku_like(text):
                break
            continue
        chosen.append(item)
        last = b
        last_text = text
        avg_conf += float(b.get("confidence") or 0.7)

    merged = ""
    for _, _, text in sorted(chosen, key=lambda x: (x[1]["y0"], x[1]["x0"])):
        if not merged:
            merged = text
        elif re.search(r"[A-Za-z0-9]$", merged) and re.match(r"[A-Za-z0-9]", text):
            merged += " " + text
        else:
            merged += text
    merged = re.sub(r"\s+", " ", merged).strip()[:300]
    return merged or _fallback_name(lines), min(0.95, avg_conf / len(chosen)), merged


def _fallback_name(lines: list[str]) -> str:
    best = ""
    best_score = -1
    for line in lines:
        text = _clean_text(line)
        if not text or _is_action_text(text) or _is_sku_like(text):
            continue
        score = len(re.findall(r"[\u4e00-\u9fff]", text)) * 3 + len(text)
        if score > best_score:
            best_score = score
            best = text
    if best:
        return best[:300]
    for line in lines:
        text = _clean_text(line)
        if text:
            return text[:300]
    return ""


def _extract_model(lines: list[str], flat: str, codes: list[str], part_no: str, merchant_code: str, blocks: list[dict]) -> tuple[str, int | None, float]:
    barcode = _parse_barcode_payload(codes)
    if barcode.get("model"):
        return barcode["model"], None, 0.98

    m = MODEL_LABEL_RE.search(flat)
    if m:
        return m.group(1).strip(), _find_line(lines, m.group(1)), 0.95

    candidates = []
    for i, line in enumerate(lines):
        text = _clean_text(line)
        if not text or _is_action_text(text):
            continue
        for tok in MODEL_TOKEN_RE.findall(text):
            tok = tok.strip("._/-")
            if len(tok) < 3 or len(tok) > 40:
                continue
            if not re.search(r"[A-Za-z]", tok) or not re.search(r"\d", tok):
                continue
            if tok.upper() in SKIP_TOKENS:
                continue
            if _is_package_token(tok) or _is_unit_token(tok):
                continue
            if re.search(r"\d+x\d+P|\d+(?:\.\d+)?mm", tok, re.IGNORECASE):
                continue
            if CCODE_RE.fullmatch(tok) or PVCODE_RE.fullmatch(tok):
                continue
            if merchant_code and _norm_key(tok) == _norm_key(merchant_code):
                continue
            if part_no and _norm_key(tok) == _norm_key(part_no):
                continue
            if SIZE_LIST_RE.fullmatch(tok) or RANGE_MODEL_RE.fullmatch(tok) or RATING_RE.fullmatch(tok):
                continue
            score = 0
            if any(ch.isupper() for ch in tok):
                score += 2
            if "-" in tok or "/" in tok:
                score += 2
            if len(tok) >= 6:
                score += 1
            if tok[0].isalpha():
                score += 1
            if LABEL_CODE_RE.fullmatch(tok):
                score += 2
            if PREFERRED_MODEL_RE.fullmatch(tok):
                score += 4
            candidates.append((score, i, tok, i))

    if not candidates:
        return "", None, 0.0
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    score, _, tok, i = candidates[0]
    base = 0.9 if PREFERRED_MODEL_RE.fullmatch(tok) else (0.8 if LABEL_CODE_RE.fullmatch(tok) else 0.6)
    return tok, i, min(0.95, base * max(0.5, _line_conf(blocks, i, 0.85)))


def _canon_package(matches: list[str], model: str) -> str:
    vals = []
    seen = set()
    for x in matches:
        x = x.strip()
        key = _norm_key(x)
        if not key or key in seen:
            continue
        seen.add(key)
        vals.append(x)
    if not vals:
        return ""
    pin = next((x for x in vals if re.fullmatch(r"\d+x\d+P", x, re.IGNORECASE)), "")
    pitch = next((x for x in vals if re.fullmatch(r"\d+(?:\.\d+)?\s*mm", x, re.IGNORECASE)), "")
    if pin and pitch:
        return f"{pin} {pitch.lower()}"
    if pin:
        return pin
    metric = next((x for x in vals if re.fullmatch(r"0850|1040|1265|1770", x, re.IGNORECASE)), "")
    if metric:
        return metric
    if any(re.fullmatch(r"OP-?\d+", x, re.IGNORECASE) for x in vals) and model.upper().startswith("STM32"):
        return "TSSOP-20"
    return vals[0]


def _extract_package(lines: list[str], model: str) -> tuple[str, int | None, float, str]:
    candidates = []
    for i, line in enumerate(lines):
        text = _clean_text(line)
        if not text or _is_action_text(text):
            continue
        matches = _find_package_matches(text)
        if not matches:
            continue
        score = 0
        if re.search(r"封装|package|P\s*=|间距|pin|PIN|尺寸", text, re.IGNORECASE):
            score += 3
        if re.search(r"\d+x\d+P", text, re.IGNORECASE):
            score += 3
        if re.search(r"\d+(?:\.\d+)?\s*mm", text, re.IGNORECASE):
            score += 2
        if re.search(r"[（(\[【;；]", text):
            score += 1
        if text.count("/") >= 3:
            score -= 2
        if re.search(r"0850|1040|1265|1770", text):
            score += 1
        candidates.append((score, i, matches, i, text))
    if not candidates:
        return "", None, 0.0, ""
    candidates.sort(key=lambda x: (-x[0], x[1]))
    score, _, matches, i, text = candidates[0]
    pkg = _canon_package(matches, model)
    conf = 0.9 if score >= 3 else (0.78 if score >= 1 else 0.62)
    return pkg, i, conf, text


def _score_codes(lines: list[str], pattern: re.Pattern) -> tuple[str, int | None, str]:
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if m:
            return m.group(1).strip(), i, line
    return "", None, ""


def _extract_part_no(lines: list[str], flat: str, codes: list[str]) -> tuple[str, int | None, float, str]:
    barcode = _parse_barcode_payload(codes)
    if barcode.get("part_no"):
        return barcode["part_no"], None, 0.98, "二维码载荷 pc"

    val, i, line = _score_codes(lines, MERCHANT_CODE_RE)
    if val:
        return val, i, 0.88, line

    for code in codes or []:
        c = str(code or "").strip()
        if CCODE_RE.fullmatch(c) or PVCODE_RE.fullmatch(c):
            return c, None, 0.95, c
    m = CCODE_RE.search(flat)
    if m:
        return m.group(0), _find_line(lines, m.group(0)), 0.9, m.group(0)
    m = PVCODE_RE.search(flat)
    if m:
        return m.group(0), _find_line(lines, m.group(0)), 0.95, m.group(0)
    return "", None, 0.0, ""


def _extract_price(lines: list[str], flat: str) -> tuple[str, int | None, float, str]:
    for i, line in enumerate(lines):
        for p in PRICE_RE:
            m = p.search(line)
            if m:
                return m.group(1), i, 0.9, line
    for p in PRICE_RE:
        m = p.search(flat)
        if m:
            return m.group(1), _find_line(lines, m.group(0)), 0.75, m.group(0)
    return "", None, 0.0, ""


def _format_qty(value: float) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:g}"


def _analyze(lines: list[str], codes: list[str], blocks: list[dict]) -> dict:
    lines = [str(x or "").strip() for x in lines if str(x or "").strip()]
    flat = " ".join(lines)
    source = _detect_source(lines, codes)
    barcode = _parse_barcode_payload(codes)

    merchant, merchant_i, merchant_line = _score_codes(lines, MERCHANT_CODE_RE)
    part_no, part_i, part_conf, part_ev = _extract_part_no(lines, flat, codes)
    model, model_i, model_conf = _extract_model(lines, flat, codes, part_no, merchant, blocks)
    package, package_i, package_conf, package_ev = _extract_package(lines, model)
    price, price_i, price_conf, price_ev = _extract_price(lines, flat)

    spec_qty, spec_i, spec_ev, spec_ambiguous = _extract_spec_qty(lines)
    purchase_qty, purchase_i, purchase_ev = _extract_purchase_qty(lines)
    label_qty, label_i, label_ev = _extract_label_qty(lines)

    if source == "label":
        qty = label_qty if label_qty is not None else (barcode.get("qty") if barcode.get("qty") is not None else (spec_qty or 1.0))
        purchase_for_formula = 1.0
        spec_for_formula = qty
        qty_i = label_i if label_i is not None else spec_i
        qty_ev = label_ev or spec_ev or "标签数量"
        formula = f"标签数量 {_format_qty(qty)}"
        qty_conf = 0.95 if (label_qty is not None or barcode.get("qty") is not None) else 0.65
    elif source == "purchase":
        purchase_for_formula = purchase_qty if purchase_qty is not None else 1.0
        spec_for_formula = spec_qty
        qty = (spec_qty if spec_qty is not None else 1.0) * purchase_for_formula
        qty_i = purchase_i if purchase_i is not None else spec_i
        qty_ev = purchase_ev or spec_ev
        if spec_qty is not None:
            formula = f"规格 {_format_qty(spec_qty)} × 购买 {_format_qty(purchase_for_formula)} = {_format_qty(qty)}"
            qty_conf = 0.93 if purchase_qty is not None else 0.82
        else:
            formula = f"购买 {_format_qty(purchase_for_formula)}"
            qty_conf = 0.8 if purchase_qty is not None else 0.5
    else:
        if label_qty is not None:
            spec_for_formula = label_qty
            purchase_for_formula = 1.0
            qty = label_qty
            qty_i = label_i
            qty_ev = label_ev
            formula = f"标签数量 {_format_qty(qty)}"
            qty_conf = 0.8
        elif spec_qty is not None:
            spec_for_formula = spec_qty
            purchase_for_formula = purchase_qty if purchase_qty is not None else 1.0
            qty = spec_qty * purchase_for_formula
            qty_i = purchase_i if purchase_i is not None else spec_i
            qty_ev = purchase_ev or spec_ev
            formula = f"规格 {_format_qty(spec_qty)} × 购买 {_format_qty(purchase_for_formula)} = {_format_qty(qty)}"
            qty_conf = 0.72 if purchase_qty is not None else 0.55
        elif purchase_qty is not None:
            spec_for_formula = None
            purchase_for_formula = purchase_qty
            qty = purchase_qty
            qty_i = purchase_i
            qty_ev = purchase_ev
            formula = f"购买 {_format_qty(qty)}"
            qty_conf = 0.65
        else:
            spec_for_formula = None
            purchase_for_formula = 1.0
            qty = 1.0
            qty_i = None
            qty_ev = ""
            formula = "未识别到数量，默认 1"
            qty_conf = 0.3

    if spec_ambiguous:
        qty_conf *= 0.8
    qty_conf = max(0.0, min(0.99, qty_conf * max(0.55, _line_conf(blocks, qty_i, 0.8))))

    name, name_conf, name_ev = _merge_title(blocks, lines)
    brand = ""
    brand_i = None
    m = BRAND_RE.search(flat)
    if m:
        brand = m.group(1)
        brand_i = _find_line(lines, brand)

    lot_no = ""
    m = LOT_RE.search(flat)
    if m:
        lot_no = m.group(1)
    date_code = ""
    m = DATE_RE.search(flat)
    if m:
        date_code = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    fields = {
        "supplier_part_no": part_no,
        "model": model,
        "name": name,
        "brand": brand,
        "package": package,
        "qty": _format_qty(qty),
        "price": price,
        "lot_no": lot_no,
        "date_code": date_code,
        "codes": codes,
    }

    confidence = {
        "supplier_part_no": round(part_conf, 3),
        "model": round(model_conf, 3),
        "name": round(name_conf, 3),
        "brand": round(0.85 if brand else 0.0, 3),
        "package": round(package_conf, 3),
        "qty": round(qty_conf, 3),
        "price": round(price_conf, 3),
        "lot_no": 0.8 if lot_no else 0.0,
        "date_code": 0.8 if date_code else 0.0,
    }
    evidence = {
        "supplier_part_no": {"text": part_ev, "reason": "料号/二维码"},
        "model": {"text": model, "line": model_i},
        "name": {"text": name_ev},
        "brand": {"text": lines[brand_i] if brand_i is not None else ""},
        "package": {"text": package_ev, "line": package_i},
        "qty": {"text": qty_ev, "formula": formula, "line": qty_i},
        "price": {"text": price_ev, "line": price_i},
    }
    qty_detail = {
        "spec_qty": spec_for_formula,
        "purchase_qty": purchase_for_formula,
        "label_qty": label_qty,
        "qty": qty,
        "formula": formula,
        "source": source,
        "ambiguous": spec_ambiguous,
    }
    return {
        "source": source,
        "fields": fields,
        "confidence": confidence,
        "evidence": evidence,
        "qty_detail": qty_detail,
    }
