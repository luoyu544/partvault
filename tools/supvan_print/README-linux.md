# Linux 直连打印

`supvan-print-helper` 是 `SupvanPrintHelper.exe` 的 Linux 替代实现，接口完全一致：

```
supvan-print-helper status <out.json>
supvan-print-helper print <job.json> <out.json>
```

## 来源

协议实现来自开源项目 [heeen/supvan-cups](https://github.com/heeen/supvan-cups)（MIT）。
本目录的 `supvan-print-helper.rs` 是该仓库 `crates/supvan-print-helper/src/main.rs` 的副本，
并在 `supvan-proto` 中增加了 `Printer::print_buffers_at_speed()`。

## 与 Windows 官方 SDK 对齐的三个要点

1. **按打印头原生分辨率出图**
   标签布局按 50×30mm 定义，T50 打印头是 384 点 / 48mm。网页端现在用
   `render_png_for_print(layout, 384)` 直接按 384 像素宽绘制，不再先画 400 像素再缩放。
   缩放是之前小字发虚的主要原因。

2. **阈值二值化，而不是抖动**
   标签内容是文字、二维码和条码，不是照片。灰度图先按阈值转成纯黑白，
   再交给协议层，避免 Bayer 抖动把细笔画打散成噪点。

3. **固定打印速度 40**
   官方 SDK 的 `PrintSet.Speed = 40`。开源库默认按压缩后数据量自动选速度，
   稀疏页面会选到 60，每个点受热时间变短，细笔画偏淡。

## 可调参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SUPVAN_THRESHOLD` | `150` | 二值化阈值，0–255。字太细调低（如 128），太粗/糊调高（如 180） |
| `SUPVAN_DENSITY` | `4` | 打印浓度，对应 SDK 的 `Deepness`，范围 0–15 |
| `SUPVAN_PRINT_HELPER` | — | 服务端指向本程序的路径 |
| `SUPVAN_PRINTER_DEVICE` | 自动 | 指定 `/dev/hidrawN`，默认按 VID `0x1820` 自动查找 |

参数写在 `/etc/systemd/system/partvault.service` 的 `Environment=` 行里，改完
`systemctl daemon-reload && systemctl restart partvault`。

## 重新编译

```bash
cd /home/luoyu/supvan-cups
CARGO_HOME=/home/luoyu/.cargo cargo build --release -p supvan-print-helper
cp target/release/supvan-print-helper /home/luoyu/partvault/tools/supvan_print/
```

## udev 权限

`/etc/udev/rules.d/99-supvan-printer.rules`：

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1820", MODE="0660", GROUP="plugdev"
```

服务用户需要在 `plugdev` 组内。
