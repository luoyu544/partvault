# 元件库管家 PartVault

电子元器件库存与库位管理系统。部署在本机或局域网服务器，用浏览器访问，数据保存在本地 SQLite 文件中，不依赖互联网。

## 功能

- **物料管理**：型号、名称、品牌、封装、分类、供应商料号、购买链接、安全库存、多来源价格
- **库位管理**：仓库 / 柜 / 层 / 盒（格）多级库位树，批量新建、连续编号、批量移库、带占用检查的删除
- **入库**：手动入库、拍照 OCR 识别、Excel/CSV 批量导入、扫码入库，按批次记录数量、日期码和单价
- **出库与借还**：消耗出库、借出、归还，借出生成未归还记录，归还可分批核销
- **标签打印**：50×30mm 物料标签与库位标签，支持 USB 直连打印、A4 PDF、Supvan `.sef`
- **扫码**：USB / 串口扫码枪、手机摄像头、拍照识别；先扫物料签再扫库位签完成出入库
- **BOM 核对**：导入 BOM 后按料号/型号匹配库存，列出缺料，支持按 BOM 出库
- **盘点与流水**：按库位盘点生成差异流水，全部库存变动可查询
- **数据管理**：完整备份下载与恢复、图片归档、局域网多设备访问

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI + Uvicorn |
| 模板 | Jinja2 |
| 数据库 | SQLite（WAL 模式） |
| ORM | SQLAlchemy 2 |
| 前端 | 原生 HTML / CSS / JavaScript |
| 图像与标签 | Pillow、qrcode、reportlab |
| 识别 | RapidOCR（onnxruntime）、zxing-cpp |
| 导入导出 | openpyxl |

库存写入通过线程锁 + 跨进程文件锁串行化，批次、流水和借出记录在同一事务中更新，保证账面数量与流水一致。

## 目录结构

```
app/
  main.py            路由与页面
  models.py          SQLAlchemy 模型
  db.py              数据库连接与初始化
  services/          库存、库位、标签、OCR、导入、备份等
  templates/         Jinja2 页面
  static/            CSS / JS / 图标
templates/           标签模板（label_50x30.sef）
tests/               单元与集成测试
tools/supvan_print/  硕方 T50/T80 直连打印组件
deploy/              Linux 服务器 systemd 与 nginx 配置
run.py               本地启动入口（Windows 双击体验）
```

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -r requirements.txt
python run.py                   # 打开 http://127.0.0.1:8765
```

首次启动会在 `data/` 下创建数据库并写入默认分类、库位和供应商。

## Linux 服务器部署

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

生产环境用 systemd 托管，nginx 反向代理 80/443：

- `deploy/partvault.service` → `/etc/systemd/system/partvault.service`
- `deploy/partvault.nginx` → `/etc/nginx/sites-available/partvault`

手机摄像头扫码要求 HTTPS（`getUserMedia` 仅在安全上下文可用），nginx 配置里已带自签名证书示例。

## 标签打印

**Windows**：`tools/supvan_print/SupvanPrintHelper.exe` 调用硕方官方 SDK 通过 USB HID 直连打印。SDK 的 DLL 与编译好的 exe 未纳入仓库，请从硕方官网获取后放入该目录，或用 `build.ps1` 重新编译。

**Linux**：`tools/supvan_print/supvan-print-helper.rs` 是基于开源协议实现 [heeen/supvan-cups](https://github.com/heeen/supvan-cups)（MIT）的替代实现，接口与原 exe 完全一致。为对齐官方 SDK 的打印清晰度，标签按打印头原生 384 点宽度绘制、以阈值二值化代替抖动、并把打印速度固定为 SDK 的 40（详见 `tools/supvan_print/README-linux.md` 与 `supvan-proto-print-at-speed.patch`）。编译后通过环境变量指向它：

```bash
SUPVAN_PRINT_HELPER=/path/to/supvan-print-helper
```

需要 udev 规则让服务用户访问 hidraw 设备：

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1820", MODE="0660", GROUP="plugdev"
```

无论哪种方式，A4 PDF 与 `.sef` 导出都不依赖打印组件。

## 数据与备份

- 数据库：`data/partvault.db`
- 上传图片：`data/uploads/`
- 备份：`data/backups/`，也可在设置页直接下载完整 zip 备份

`data/` 已在 `.gitignore` 中排除，仓库不包含任何真实库存数据。

## 测试

```bash
pytest tests -q
```
