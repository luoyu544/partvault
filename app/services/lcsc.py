"""立创商城在线搜索。

数据来自立创 EDA Pro 编辑器使用的国内站商品搜索接口：
https://pro.lceda.cn/api/eda/product/search

这个接口不是 LCSC 官方 OpenAPI，不需要 key，但也没有 SLA；所以这里只做
按需查询、短时内存缓存、限速和失败降级，不落库、不批量抓取。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from difflib import SequenceMatcher
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..models import Part, PartSource, Supplier

SEARCH_URL = "https://pro.lceda.cn/api/eda/product/search"
PRODUCT_BASE = "https://item.szlcsc.com"
DATASHEET_BASE = "https://atta.szlcsc.com"
MANUAL_SEARCH = "https://so.szlcsc.com/global.html?k="
SOURCE_LABEL = "立创商城（国内站 · EDA 商品搜索）"
CACHE_TTL = 60.0
CACHE_MAX = 128
MIN_INTERVAL = 0.8
HTTP_TIMEOUT = 10
CODE_RE = re.compile(r"^C\d{4,}$", re.IGNORECASE)
EMBEDDED_CODE_RE = re.compile(r"(?<![A-Z0-9])C(\d{4,})(?![A-Z0-9])", re.IGNORECASE)

_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


class LcscError(Exception):
    """立创查询失败，给用户看的错误。"""


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def extract_code(value) -> str:
    """从扫码结果中提取立创 C 码。

    立创包装袋上的条码/二维码有时是纯 ``C1525``，有时带前缀、URL
    或数量，例如 ``LCSC:C1525``、``...?code=C1525&qty=100``。只在
    明确的词边界上提取，避免把普通型号里的字母 C 误当成料号。
    """
    text = _text(value).upper()
    if not text:
        return ""
    if CODE_RE.fullmatch(text):
        return text
    match = EMBEDDED_CODE_RE.search(text)
    return f"C{match.group(1)}" if match else ""


def _int(value, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _float(value, default=None):
    try:
        result = float(str(value).strip())
        return result
    except (TypeError, ValueError):
        return default


def _norm(value) -> str:
    """用于比较的规范化文本：小写、去空白、统一常用单位。"""
    s = _text(value).lower()
    s = s.replace("μ", "u").replace("µ", "u")
    s = s.replace("Ω", "ohm").replace("ω", "ohm")
    s = re.sub(r"\s+", "", s)
    return s


def _token_text(value) -> str:
    """保留词边界，只把“100 nF”这种数字和单位之间的空格去掉。"""
    s = _text(value).lower()
    s = s.replace("μ", "u").replace("µ", "u")
    s = s.replace("Ω", "ohm").replace("ω", "ohm")
    s = re.sub(
        r"(?<=\d)\s+(?=(?:pf|nf|uf|mf|f|r|k|m|ohm|v|mv|a|ma|w|mw|hz|khz|mhz|%|ppm)\b)",
        "",
        s,
    )
    return s


def clean_query(query: str) -> str:
    s = _text(query).replace("\u3000", " ")
    s = re.sub(r"^(?:商品名称|商品型号|产品型号|型号|名称|规格|封装|品牌)\s*[:：]\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;，；")
    return s[:120]


def build_query(name: str = "", model: str = "", package: str = "", brand: str = "") -> str:
    """从 OCR/物料字段生成一个适合搜索的初始关键词。"""
    model = clean_query(model)
    name = clean_query(name)
    package = clean_query(package)
    brand = clean_query(brand)
    if model and len(model) >= 2 and not re.fullmatch(r"[\d.%]+", model):
        query = model
    elif name:
        query = name
    else:
        query = " ".join(x for x in (brand, package) if x)
    # C 码必须保持“单独一个词”，否则会变成模糊搜索。
    if package and not CODE_RE.fullmatch(clean_query(query)) and _norm(package) not in _norm(query):
        query = f"{query} {package}".strip()
    return clean_query(query)


def manual_url(query: str = "") -> str:
    return MANUAL_SEARCH + quote(clean_query(query))


def _rate_limit() -> None:
    global _LAST_REQUEST
    with _RATE_LOCK:
        wait = MIN_INTERVAL - (time.monotonic() - _LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()


def _cache_get(key: str):
    now = time.monotonic()
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if not row:
            return None
        created, payload = row
        if now - created > CACHE_TTL:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return payload


def _cache_put(key: str, payload: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), payload)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _http_json(url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": "https://pro.lceda.cn/editor",
        "Origin": "https://pro.lceda.cn",
    }
    last_error = None
    for attempt in range(2):
        _rate_limit()
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "ignore"))
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            last_error = f"HTTP {exc.code}"
            if exc.code in (400, 401, 403, 404):
                raise LcscError(f"立创搜索接口返回 {last_error}") from exc
            if body:
                last_error = f"{last_error} {body}"
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc)
        if attempt == 0:
            time.sleep(0.35)
    raise LcscError(f"无法连接立创搜索接口：{last_error or '未知错误'}")


def _fetch_products(keyword: str, page: int = 1, page_size: int = 30):
    params = {
        "keyword": keyword,
        "type": "3",
        "page": str(max(1, page)),
        "pageSize": str(max(1, min(page_size, 100))),
    }
    data = _http_json(f"{SEARCH_URL}?{urlencode(params)}")
    if not isinstance(data, dict):
        raise LcscError("立创搜索接口返回格式异常")
    result = data.get("result")
    if result is None:
        code = _int(data.get("code"), 0)
        if code not in (0, 200):
            message = _text(data.get("msg") or data.get("message") or "接口没有返回结果")
            raise LcscError(f"立创搜索失败：{message}")
        return [], 0
    if not isinstance(result, dict):
        raise LcscError("立创搜索接口返回格式异常")
    products = result.get("productList")
    if products is None:
        products = []
    if not isinstance(products, list):
        raise LcscError("立创搜索接口返回格式异常")
    total = _int(result.get("total"), len(products))
    return products, total


PARAM_SKIP = {
    "Name", "Datasheet", "Supplier", "Supplier Part", "LCSC Part Name",
    "Add into BOM", "Convert to PCB", "Symbol", "Footprint", "Designator",
    "3D Model", "3D Model Title", "3D Model Transform",
}

PARAM_LABELS = {
    "Manufacturer": "制造商",
    "Manufacturer Part": "制造商型号",
    "Supplier Footprint": "封装",
    "JLCPCB Part Class": "嘉立创分类",
    "Type": "类型",
    "Value": "值",
    "Tolerance": "精度",
    "Power(Watts)": "功率",
    "Voltage-Supply(Max)": "耐压",
    "Temperature Coefficient": "温度系数",
    "Operating Temperature": "工作温度",
    "Resistance": "阻值",
    "Capacitance": "容值",
    "Inductance": "电感",
    "Current Rating": "额定电流",
    "Voltage Rating": "额定电压",
}


def _normalize_params(raw: dict) -> "OrderedDict[str, str]":
    out: "OrderedDict[str, str]" = OrderedDict()
    product_params = raw.get("productParams")
    rows = []
    if isinstance(product_params, dict):
        rows = list(product_params.items())
    elif isinstance(product_params, list):
        for row in product_params:
            if isinstance(row, dict):
                rows.append((row.get("paramName") or row.get("name") or "", row.get("paramValue") or row.get("value") or ""))
    for key, value in rows:
        key, value = _text(key), _text(value)
        if key and value and key not in PARAM_SKIP:
            out.setdefault(PARAM_LABELS.get(key, key), value)

    device = raw.get("device_info") or {}
    attrs = device.get("attributes") if isinstance(device, dict) else None
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            key, value = _text(key), _text(value)
            if key and value and key not in PARAM_SKIP:
                out.setdefault(PARAM_LABELS.get(key, key), value)
    return out


def _parse_price_tiers(raw: dict) -> list[dict]:
    tiers = []
    price_list = raw.get("priceList")
    if not isinstance(price_list, list):
        price_list = raw.get("productPriceList")
    if not isinstance(price_list, list):
        return tiers
    for row in price_list:
        qty = 0
        price = None
        if isinstance(row, dict):
            qty = _int(row.get("startNumber") or row.get("ladder") or row.get("minQty") or row.get("ladderLevel"))
            price = _float(row.get("price"))
            if price is None:
                price = _float(row.get("productPrice"))
            if price is None:
                price = _float(row.get("cnyProductPrice"))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            qty = _int(row[0])
            price = _float(row[1])
        if price is not None and price >= 0:
            tiers.append({"qty": qty, "price": price})
    tiers.sort(key=lambda x: (x["qty"], x["price"]))
    return tiers


def _normalize_item(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    code = _text(raw.get("code") or raw.get("productCode")).upper()
    if not code:
        return None
    name = _text(raw.get("name") or raw.get("productName"))
    mpn = _text(raw.get("model") or raw.get("productModel") or raw.get("lightProductModel"))
    brand = _text(raw.get("brandName") or raw.get("brandNameEn") or raw.get("lightBrandName"))
    package = _text(raw.get("standard") or raw.get("encapStandard") or raw.get("lightStandard"))
    category = _text(raw.get("catalogName") or raw.get("lightCatalogName"))
    stock = _int(raw.get("stockNumber") or raw.get("validStockNumber") or raw.get("stock"))
    tiers = _parse_price_tiers(raw)
    price_min = min((t["price"] for t in tiers), default=None)

    image = _text(raw.get("image") or raw.get("breviaryImageUrl"))
    image_url = ""
    if image:
        image_url = image.split("<$>")[0].strip()
        if image_url.startswith("//"):
            image_url = "https:" + image_url

    pdf = _text(raw.get("pdfFileUrl") or raw.get("pdfHtmlUrl") or raw.get("pdfUrl"))
    datasheet = ""
    if pdf:
        if pdf.startswith("//"):
            datasheet = "https:" + pdf
        elif pdf.startswith("/"):
            datasheet = DATASHEET_BASE + pdf
        else:
            datasheet = pdf

    product_id = _int(raw.get("id") or raw.get("productId"))
    if product_id:
        product_url = f"{PRODUCT_BASE}/{product_id}.html"
    else:
        product_url = MANUAL_SEARCH + quote(code)

    device = raw.get("device_info") if isinstance(raw.get("device_info"), dict) else {}
    device_uuid = _text(raw.get("hasDevice") or device.get("uuid"))
    if not datasheet:
        device_attrs = device.get("attributes") if isinstance(device.get("attributes"), dict) else {}
        datasheet = _text(device_attrs.get("Datasheet"))
    description = _text(raw.get("desc") or device.get("description") or device.get("Description"))
    attrs = _normalize_params(raw)
    if not description and attrs:
        description = "；".join(f"{k}: {v}" for k, v in list(attrs.items())[:8])

    attr_text = "\n".join(f"{k}: {v}" for k, v in attrs.items())
    return {
        "code": code,
        "name": name or mpn or code,
        "mpn": mpn,
        "brand": brand,
        "package": package,
        "category": category,
        "description": description,
        "stock": stock,
        "price_tiers": tiers,
        "price_min": price_min,
        "product_url": product_url,
        "datasheet_url": datasheet,
        "image_url": image_url,
        "attributes": dict(attrs),
        "attributes_text": attr_text,
        "device_uuid": device_uuid,
        "has_device": bool(device_uuid),
        "jlc_class": _text(raw.get("JLCPCB Part Class") or (attrs.get("嘉立创分类") if attrs else "")),
        "source": SOURCE_LABEL,
    }


PACKAGE_RE = re.compile(
    r"(?<![a-z0-9])(?:0201|0402|0603|0805|1206|1210|1812|2010|2512|"
    r"sot-?23(?:-\d+)?|tsot-?23(?:-\d+)?|sod-?123|sod-?323|"
    r"tssop-?\d+|ssop-?\d+|msop-?\d+|soic-?\d+|sop-?\d+|"
    r"qfn-?\d+|dfn-?\d+|lqfp-?\d+|tqfp-?\d+|bga-?\d+|"
    r"to-?220|to-?92|to-?252|to-?263|do-?214|do-?41|do-?35|"
    r"dip-?\d+|plcc-?\d+|"
    r"\d+x\d+p|\d+(?:\.\d+)?mm|\d+\*\d+\*\d+)(?![a-z0-9])",
    re.IGNORECASE,
)
KEY_TOKEN_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:pf|nf|uf|mf|f|r|k|m|ohm|v|mv|a|ma|w|mw|hz|khz|mhz|%|ppm)"
    r"|x5r|x7r|c0g|np0|y5v)$",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[a-z0-9%]+(?:[.\-/][a-z0-9%]+)*|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
STOPWORDS = {
    "贴片", "直插", "原装", "全新", "商品", "元件", "电子", "商城", "立创", "型号",
    "规格", "封装", "品牌", "名称", "库存", "现货", "批发", "芯片", "元器件",
}


def _tokens(value: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(_token_text(value)) if t not in STOPWORDS]


def _key_params(query: str) -> list[str]:
    found = []
    for token in _tokens(query):
        if KEY_TOKEN_RE.fullmatch(token) and token not in found:
            found.append(token)
    return found


def _has_key(text: str, key: str) -> bool:
    # 不用简单子串：避免 50V 命中 150V。
    return re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text) is not None


def rank_results(query: str, items: list[dict], package: str = "", brand: str = "") -> list[dict]:
    """按 C 码/型号精确匹配优先，再按名称、封装、关键参数相似度排序。"""
    q_norm = _norm(query)
    q_upper = clean_query(query).upper()
    q_tokens = _tokens(query)
    q_keys = _key_params(query)
    pkg_filter = _norm(package)
    brand_filter = _norm(brand)
    q_packages = [m.group(0) for m in PACKAGE_RE.finditer(_token_text(query))]

    ranked = []
    for item in items:
        code = item.get("code", "")
        mpn = item.get("mpn", "")
        name = item.get("name", "")
        item_pkg = item.get("package", "")
        item_brand = item.get("brand", "")
        attrs = item.get("attributes") or {}
        search_text = _norm(" ".join([
            name, mpn, item_pkg, item_brand, item.get("category", ""),
            item.get("description", ""), " ".join(f"{k} {v}" for k, v in attrs.items()),
        ]))
        name_text = _norm(" ".join([name, mpn, item_pkg]))
        exact_code = bool(q_upper and code.upper() == q_upper)
        exact_mpn = bool(q_norm and _norm(mpn) == q_norm)
        score = 15.0

        if exact_code:
            score += 1000
        if exact_mpn:
            score += 900
        elif q_norm and mpn and (_norm(mpn) in q_norm or q_norm in _norm(mpn)):
            score += 120

        if q_norm and q_norm in name_text:
            score += 70
        ratio = SequenceMatcher(None, q_norm, name_text).ratio() if q_norm and name_text else 0
        score += ratio * 40
        hits = [t for t in q_tokens if t in search_text]
        if q_tokens:
            score += (len(hits) / len(q_tokens)) * 45

        warnings = []
        pkg_ok = True
        if item_pkg:
            if pkg_filter:
                if pkg_filter == _norm(item_pkg) or pkg_filter in _norm(item_pkg) or _norm(item_pkg) in pkg_filter:
                    score += 18
                else:
                    score -= 30
                    pkg_ok = False
                    warnings.append(f"封装不一致：要求 {package}，结果 {item_pkg}")
            for q_pkg in q_packages:
                if q_pkg == _norm(item_pkg):
                    score += 14
                elif q_pkg in _norm(item_pkg) or _norm(item_pkg) in q_pkg:
                    score += 8
                elif not pkg_filter:
                    pkg_ok = False
                    warnings.append(f"封装可能不一致：{q_pkg} → {item_pkg}")
                    break
        elif pkg_filter or q_packages:
            pkg_ok = False
            warnings.append("结果未提供封装")

        if brand_filter and item_brand:
            if brand_filter == _norm(item_brand) or brand_filter in _norm(item_brand) or _norm(item_brand) in brand_filter:
                score += 12
            else:
                score -= 8
                warnings.append(f"品牌不一致：要求 {brand}，结果 {item_brand}")
        elif brand_filter:
            score -= 4
            warnings.append("结果未提供品牌")

        missing_keys = [k for k in q_keys if not _has_key(search_text, k)]
        if missing_keys:
            score -= min(24, 6 * len(missing_keys))
            warnings.append("关键参数可能不一致：" + "、".join(missing_keys[:4]))

        if int(item.get("stock") or 0) > 0:
            score += 4
        if exact_code:
            reason = "C 码精确匹配"
        elif exact_mpn:
            reason = "型号精确匹配"
        elif q_tokens and len(hits) == len(q_tokens):
            reason = "名称/参数全部命中"
        elif hits:
            reason = f"名称/参数命中 {len(hits)}/{len(q_tokens)}"
        else:
            reason = "名称相近"

        out = dict(item)
        out.update({
            "match_score": round(score, 1),
            "match_reason": reason,
            "warnings": warnings,
            "is_exact_code": exact_code,
            "is_exact_mpn": exact_mpn,
            "recommended": bool((exact_code or exact_mpn) and not warnings),
            "package_ok": pkg_ok,
        })
        ranked.append(out)

    ranked.sort(key=lambda x: (
        0 if x["is_exact_code"] else 1,
        0 if x["is_exact_mpn"] else 1,
        -x["match_score"],
        -int(x.get("stock") or 0),
        x.get("code", ""),
    ))
    return ranked


def search(
    query: str,
    package: str = "",
    brand: str = "",
    limit: int = 20,
    refresh: bool = False,
) -> dict:
    q = clean_query(query)
    if not q:
        raise LcscError("请输入型号、名称或 C 码")
    limit = max(1, min(_int(limit, 20), 50))
    cache_key = f"{q}|{_norm(package)}|{_norm(brand)}|{limit}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            payload = dict(cached)
            payload["cached"] = True
            return payload

    q_upper = clean_query(q).upper()
    is_code_query = bool(CODE_RE.fullmatch(q_upper))
    # C 码查询多取一些，确保精确结果不会因为接口模糊排序而漏掉。
    raw_items, total = _fetch_products(
        q, page=1, page_size=100 if is_code_query else max(30, limit * 3)
    )
    items = []
    seen = set()
    for raw in raw_items:
        item = _normalize_item(raw)
        if item and item["code"] not in seen:
            seen.add(item["code"])
            items.append(item)
    ranked = rank_results(q, items, package=package, brand=brand)
    if limit and len(ranked) > limit:
        ranked = ranked[:limit]

    warning = ""
    if is_code_query:
        exact_items = [x for x in ranked if x["is_exact_code"]]
        if exact_items:
            # C 码只认精确匹配：C17710 不能把 C17717710/C177104 当成同一器件。
            ranked = exact_items
        else:
            warning = f"没有找到 C 码 {q_upper} 的精确匹配，下面是相似器件。"
    exact = [x for x in ranked if x["is_exact_code"] or x["is_exact_mpn"]]
    if not ranked:
        warning = warning or "没有找到匹配器件，可以换个关键词，或打开立创商城手动查询。"
    payload = {
        "query": q,
        "package": clean_query(package),
        "brand": clean_query(brand),
        "source": SOURCE_LABEL,
        "source_url": "https://pro.lceda.cn/",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "count": len(ranked),
        "exact_count": len(exact),
        "warning": warning,
        "manual_url": manual_url(q),
        "results": ranked,
        "cached": False,
    }
    _cache_put(cache_key, payload)
    return dict(payload)


def lookup(code: str, refresh: bool = False) -> dict:
    code = clean_query(code).upper()
    if not CODE_RE.fullmatch(code):
        raise LcscError("C 码格式不正确，应类似 C17710")
    data = search(code, limit=30, refresh=refresh)
    for item in data.get("results") or []:
        if item.get("code", "").upper() == code:
            return item
    raw_items, _total = _fetch_products(code, page=1, page_size=100)
    for raw in raw_items:
        item = _normalize_item(raw)
        if item and item["code"].upper() == code:
            item.update({"match_score": 1000.0, "match_reason": "C 码精确匹配",
                         "warnings": [], "is_exact_code": True, "is_exact_mpn": False,
                         "recommended": True, "package_ok": True})
            return item
    raise LcscError(f"立创商城中没有找到 C 码 {code}")


def _parse_attributes(text: str) -> "OrderedDict[str, str]":
    out: "OrderedDict[str, str]" = OrderedDict()
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([^:：]+)[:：]\s*(.*)$", line)
        if m:
            key, value = m.group(1).strip(), m.group(2).strip()
        else:
            key, value = line, ""
        if key:
            out[key] = value
    return out


def ensure_lcsc_supplier(db) -> Supplier:
    supplier = db.query(Supplier).filter(Supplier.type == "lcsc").first()
    if supplier is None:
        supplier = Supplier(name="立创商城", type="lcsc", homepage="https://www.szlcsc.com")
        db.add(supplier)
        db.flush()
    return supplier


def apply_candidate(db, part: Part, candidate: dict, mode: str = "blank") -> dict:
    """把选中的立创器件写入物料。

    blank（默认）：只补空字段；overwrite（以立创为准）：覆盖型号/名称/品牌/
    封装/链接/参数。两种情况都不碰库存、库位、批次和实际入库单价。
    """
    overwrite = mode == "overwrite"
    changed = []
    supplier = ensure_lcsc_supplier(db)

    def set_field(attr: str, value, label: str, allow_overwrite: bool = True):
        value = _text(value)
        if not value:
            return
        current = _text(getattr(part, attr, ""))
        if (overwrite and allow_overwrite) or not current:
            if current != value:
                setattr(part, attr, value)
                changed.append(label)

    mpn = _text(candidate.get("mpn"))
    name = _text(candidate.get("name"))
    if mpn:
        set_field("model", mpn, "型号")
    elif not _text(part.model) and name:
        set_field("model", name, "型号")
    set_field("name", name, "名称")
    set_field("brand", candidate.get("brand"), "品牌")
    set_field("package", candidate.get("package"), "封装")
    set_field("purchase_url", candidate.get("product_url"), "购买链接")
    set_field("datasheet_url", candidate.get("datasheet_url"), "数据手册")

    new_attrs = candidate.get("attributes") or {}
    existing_attrs = _parse_attributes(part.attributes)
    if new_attrs:
        if overwrite:
            merged = OrderedDict((str(k), _text(v)) for k, v in new_attrs.items())
        else:
            merged = OrderedDict((str(k), _text(v)) for k, v in new_attrs.items())
            for key, value in existing_attrs.items():
                if _text(value):
                    merged[key] = value
        new_text = "\n".join(f"{k}: {v}" for k, v in merged.items() if v)
        if new_text and _text(part.attributes) != new_text:
            part.attributes = new_text
            changed.append("参数")

    code = _text(candidate.get("code")).upper()
    if not part.supplier_id:
        part.supplier_id = supplier.id
        changed.append("主供应商")
    can_set_primary_code = part.supplier_id == supplier.id
    if code and can_set_primary_code and (overwrite or not _text(part.supplier_part_no)):
        if _text(part.supplier_part_no) != code:
            part.supplier_part_no = code
            changed.append("立创料号")

    if code:
        source = db.query(PartSource).filter_by(
            part_id=part.id, supplier_id=supplier.id, supplier_part_no=code
        ).first()
        if source is None:
            source = PartSource(part_id=part.id, supplier_id=supplier.id, supplier_part_no=code)
            db.add(source)
            changed.append("立创来源")
        if candidate.get("product_url"):
            source.url = candidate["product_url"]
        if candidate.get("price_min") is not None:
            source.price = candidate["price_min"]
            source.note = f"立创参考价（{datetime.now().strftime('%Y-%m-%d')}）"
        source.currency = "CNY"
        tiers = candidate.get("price_tiers") or []
        if tiers:
            source.moq = _int(tiers[0].get("qty")) or None

    return {
        "changed": changed,
        "overwrite": overwrite,
        "code": code,
        "supplier": supplier.name,
    }


def create_part_from_candidate(db, candidate: dict, location_id: int | None = None,
                               note: str = "") -> Part:
    """用立创查询结果新建物料，供“扫 C 码直接入库”使用。

    这里只建档，不写库存；调用方随后用 stock_in 入库。立创参考价会写入
    PartSource，但不会当成实际入库单价，也不会碰批次/日期码。
    """
    from .stock import next_part_code

    code = _text(candidate.get("code")).upper()
    if not CODE_RE.fullmatch(code):
        raise LcscError("立创 C 码格式不正确")
    supplier = ensure_lcsc_supplier(db)
    model = _text(candidate.get("mpn") or candidate.get("name") or code)[:200]
    part = Part(
        code=next_part_code(db),
        model=model,
        name=_text(candidate.get("name") or model)[:300],
        brand=_text(candidate.get("brand"))[:120],
        package=_text(candidate.get("package"))[:80],
        default_location_id=location_id,
        supplier_id=supplier.id,
        supplier_part_no=code,
        purchase_url=_text(candidate.get("product_url")),
        datasheet_url=_text(candidate.get("datasheet_url")),
        note=_text(note) or f"立创扫码自动建档（{code}）",
        unit="PCS",
    )
    db.add(part)
    db.flush()
    apply_candidate(db, part, candidate, mode="overwrite")
    return part
