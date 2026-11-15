# Linux 直连打印

`supvan-print-helper` 是 `SupvanPrintHelper.exe` 的 Linux 替代实现，接口完全一致：
`supvan-print-helper status <out.json>` / `supvan-print-helper print <job.json> <out.json>`。

- 协议实现来自开源项目 heeen/supvan-cups（MIT），源码在 `/home/luoyu/supvan-cups`。
- 本目录的 `supvan-print-helper.rs` 是该项目的 `crates/supvan-print-helper/src/main.rs`。
- 重新编译：
  `cd /home/luoyu/supvan-cups && CARGO_HOME=/home/luoyu/.cargo cargo build --release -p supvan-print-helper`
  然后复制 `target/release/supvan-print-helper` 到本目录。
- 服务通过 systemd 环境变量 `SUPVAN_PRINT_HELPER` 指向本程序（见 `/etc/systemd/system/partvault.service`）。
- udev 规则 `/etc/udev/rules.d/99-supvan-printer.rules` 把 Supvan（VID 1820）的 hidraw 设备
  权限设为 `root:plugdev 0660`，`luoyu` 属于 `plugdev` 组。
- 标签按 50×30mm 渲染为 400×240，打印前缩放到打印头 384 点宽。
