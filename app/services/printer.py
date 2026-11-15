"""Supvan T50/T80 USB direct-print bridge.

The browser only talks to this FastAPI process.  A small C# helper loads the
official Supvan Windows SDK and prints through the printer's USB HID interface.
This keeps printing available on phones/tablets (including iPhone) without
depending on Web Bluetooth or HTTPS.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from ..db import BASE_DIR
from .label_layout import location_layout, part_layout, render_png

HELPER = Path(
    os.environ.get(
        "SUPVAN_PRINT_HELPER",
        str(BASE_DIR / "tools" / "supvan_print" / "SupvanPrintHelper.exe"),
    )
).expanduser()
DEFAULT_DEVICE = os.environ.get("SUPVAN_PRINTER_DEVICE", "").strip()

# 50x30mm, 203dpi, die-cut gap paper.  The layout is rendered to a 400x240 PNG
# by label_layout.py; the C# helper sends that image through the official SDK.
_HELPER_LOCK = threading.Lock()


def helper_available() -> bool:
    return HELPER.is_file()


def _job_label(layout: dict) -> dict:
    label = dict(layout)
    label["image"] = base64.b64encode(render_png(layout, 8)).decode("ascii")
    return label


def build_part_job(
    parts,
    brand: str = "元件库",
    show_price: bool = False,
    kind: str = "元件标签",
    today: str | None = None,
) -> dict:
    """Build the JSON contract consumed by SupvanPrintHelper.exe.

    Coordinates are millimetres from the top-left of the 50x30mm label.  The
    layout mirrors the Supvan ``.sef`` template (QR on the right, CODE128
    across the bottom).  A pre-rendered PNG is included so the SDK does not
    have to draw the template's 9-14 px text (it skips anything below 16 px).
    """
    labels = [
        _job_label(part_layout(part, brand=brand, show_price=show_price,
                               kind=kind, today=today))
        for part in parts
    ]
    return {
        "devicePath": DEFAULT_DEVICE,
        "copies": 1,
        "labels": labels,
    }


def build_location_job(locations, brand: str = "元件库", kind: str = "库位标签") -> dict:
    """Build a 50x30mm job for location labels (QR payload ``PV-L-<code>``)."""
    labels = [_job_label(location_layout(loc, brand=brand, kind=kind))
              for loc in locations]
    return {
        "devicePath": DEFAULT_DEVICE,
        "copies": 1,
        "labels": labels,
    }


def _run_helper(
    command: str,
    payload: dict | None = None,
    timeout: float = 30.0,
    lock_timeout: float | None = None,
) -> dict:
    if not helper_available():
        return {
            "ok": False,
            "error": f"打印组件未安装：{HELPER}",
            "devices": [],
        }

    with tempfile.TemporaryDirectory(prefix="partvault_print_") as tmp:
        tmp_path = Path(tmp)
        args = [str(HELPER), command]
        if payload is not None:
            job_path = tmp_path / "job.json"
            job_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            args.append(str(job_path))
        out_path = tmp_path / "result.json"
        args.append(str(out_path))

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if lock_timeout is None:
            acquired = _HELPER_LOCK.acquire()
        else:
            acquired = _HELPER_LOCK.acquire(timeout=lock_timeout)
        if not acquired:
            return {
                "ok": False,
                "error": "打印机正在打印，请稍后刷新状态",
                "devices": [],
            }
        try:
            try:
                proc = subprocess.run(
                    args,
                    cwd=str(HELPER.parent),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    creationflags=creationflags,
                )
            except subprocess.TimeoutExpired:
                return {
                    "ok": False,
                    "error": "打印服务超时，请检查打印机是否卡纸或离线",
                    "devices": [],
                }
            except OSError as exc:
                return {"ok": False, "error": f"无法启动打印组件：{exc}", "devices": []}
        finally:
            _HELPER_LOCK.release()

        if not out_path.is_file():
            detail = (proc.stderr or proc.stdout or "").strip()
            detail = detail[-300:] if detail else f"退出码 {proc.returncode}"
            return {"ok": False, "error": f"打印组件无响应：{detail}", "devices": []}

        try:
            data = json.loads(out_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"打印组件返回格式错误：{exc}", "devices": []}

    if not isinstance(data, dict):
        return {"ok": False, "error": "打印组件返回格式错误", "devices": []}
    if data.get("ok") and command == "status":
        data.setdefault("devicePath", DEFAULT_DEVICE or (data.get("devices") or [""])[0])
    return data


def status() -> dict:
    """Return printer discovery/status information for the labels page."""
    result = _run_helper("status", timeout=20.0, lock_timeout=2.0)
    result["available"] = helper_available()
    result["helper"] = str(HELPER)
    return result


def print_parts(
    parts,
    brand: str = "元件库",
    show_price: bool = False,
    kind: str = "元件标签",
) -> dict:
    if not parts:
        return {"ok": False, "error": "请先勾选物料", "devices": []}
    job = build_part_job(parts, brand=brand, show_price=show_price, kind=kind)
    if not job["labels"]:
        return {"ok": False, "error": "没有可打印的标签", "devices": []}
    return _run_helper("print", job, timeout=180.0, lock_timeout=30.0)


def print_locations(locations, brand: str = "元件库", kind: str = "库位标签") -> dict:
    if not locations:
        return {"ok": False, "error": "请先勾选库位", "devices": []}
    job = build_location_job(locations, brand=brand, kind=kind)
    if not job["labels"]:
        return {"ok": False, "error": "没有可打印的标签", "devices": []}
    return _run_helper("print", job, timeout=180.0, lock_timeout=30.0)
