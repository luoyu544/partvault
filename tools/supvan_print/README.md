# Supvan T50/T80 USB 打印桥

这个目录让「元件库管家」通过运行服务的主机，把 50×30mm 标签直接发送到
Supvan T50/T80 系列打印机。浏览器只访问 FastAPI，主机上的小工具再调用硕方
官方 Windows SDK，通过 USB HID 打印；不依赖 Web Bluetooth，所以 iPhone /
iPad 也可以从网页操作。

## 文件

- `SupvanPrintHelper.exe`：已编译好的控制台工具，网页服务调用它。
- `SupvanPrintHelper.cs`：源码；修改后可用 `build.ps1` 重新编译。
- `build.ps1`：用 Windows 自带的 .NET Framework 4.0 编译器重建 exe。
- `Supvan.T50PRO.SDK.dll`、`SevenZip.dll`、`zxing.dll`：硕方官方 T50Pro
  Windows SDK 文件。来源：
  `https://supvan.org.cn/Driver/SUPVAN.T50PRO.DLL.rar`。
  这些二进制文件版权归硕方所有，仅用于本机打印，请不要单独分发。

## 调用方式

```powershell
# 查看打印机和状态，结果写入 result.json
SupvanPrintHelper.exe status result.json

# 只检查 JSON 任务能否被 SDK 接受（不连接打印机）
SupvanPrintHelper.exe validate job.json result.json

# 真正打印
SupvanPrintHelper.exe print job.json result.json
```

输出都是 UTF-8 JSON。`print`/`validate` 的任务格式由
`app/services/label_layout.py` 生成：每张标签先把完整版面渲染成 400×240
单色 PNG（203dpi，8px/mm），放在 `image` 字段中（Base64）。helper 把图片写入
临时文件，再以 `IMAGE` 对象发送给 SDK。`qr`、`barcode`、`texts` 字段仍保留，
用于兼容和校验。

> 为什么不用 SDK 原生 TEXT：官方 `DoPrint` 会跳过 `字号(mm) × DPI < 16`
> 的文本对象，也就是小于 2.0mm 的字都不会印。`.sef` 模板中的规格、封装、
> 数量、LOT 等只有 1.25–1.75mm，所以原生 TEXT 会“少字”。整张渲染成图片后，
> 直接打印、A4 PDF 与 `.sef` 的版面完全一致。


## 环境变量

- `SUPVAN_PRINT_HELPER`：覆盖 helper 的完整路径。
- `SUPVAN_PRINTER_DEVICE`：主机连接多台硕方设备时，指定设备路径。先运行
  `status` 查看 `devices` 数组；一般只有一台，不需要设置。

设置环境变量后需要重启后台服务。计划任务方式启动时，建议在 Windows
“系统属性 → 环境变量”里添加，而不是只写在某个终端里。

## 重新编译

在 Windows 上运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

需要 `C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe`（Windows 10/11
自带的 .NET Framework 4.x 就有）。编译时会把同目录下的 SDK DLL 作为引用。

## 限制

- 打印机必须通过 USB 数据线连接到运行服务的主机；手机只负责网页操作。
- 不支持手机直接蓝牙连接打印机；这条路线是主机 USB 打印。
- 标签固定 50×30mm、203dpi、间隙纸 2mm，和现有 `.sef` / A4 PDF 的纸张一致。
- 打印布局以 `templates/label_50x30.sef` 为准（二维码在右、底部 CODE128），
  整张标签按 8px/mm 渲染成图片后发送；二维码/条形码内容和物料/库位编号保持一致。
- 如果打印机状态不是 `Waiting`，helper 不会强行发送，网页会显示具体原因。
