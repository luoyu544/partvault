# Linux 直连打印

`supvan-print-helper` 是 `SupvanPrintHelper.exe` 的 Linux 替代实现，接口完全一致：

```
supvan-print-helper status <out.json>
supvan-print-helper print <job.json> <out.json>
```

## 来源

协议实现来自开源项目 [heeen/supvan-cups](https://github.com/heeen/supvan-cups)（MIT）。
本目录的 `supvan-print-helper.rs` 是该仓库 `crates/supvan-print-helper/src/main.rs` 的副本，
并在 `supvan-proto` 中增加了 `Printer::print_buffers_at_speed()`（补丁见
`supvan-proto-print-at-speed.patch`）。

## 与 Windows 官方 SDK 对齐的三个要点

1. **复用官方 SDK 的半色调算法**
   `Supvan.T50PRO.SDK` 内部用 `ImgConverter`：先量化到 16 级灰度（`i * 17`），
   再用厂商误差扩散核，最后以 125 为阈值转 1bpp。开源库默认的 Bayer 抖动会把
   1mm 左右的中文细笔画打散成噪点。helper 默认改用 `DitherMode::Vendor16`，
   即厂商这套算法。

2. **宽度适配打印头**
   标签按 50mm 宽渲染成 400 px，T50 打印头是 384 点（48mm）。helper 用 Lanczos
   把宽度缩到 384，高度保持 240，等价于 SDK 的 `MaxDotValue = 384`。

3. **固定打印速度 40**
   官方 SDK 的 `PrintSet.Speed = 40`。开源库按压缩后数据量自动选速度，稀疏页面
   会选到 60，每个点受热时间变短，细笔画偏淡。

## 可调参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SUPVAN_DITHER` | `vendor16` | 半色调算法：`vendor16`（官方同款）/ `bayer` / `floyd-steinberg` / `atkinson` |
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
