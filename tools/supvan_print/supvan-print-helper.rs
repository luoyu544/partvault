//! Linux drop-in replacement for SupvanPrintHelper.exe.
//!
//! Accepts the same job JSON the web app already builds:
//!   {"devicePath": "", "copies": 1, "labels": [{"image": "<base64 png>", ...}]}
//! and prints each label through the Supvan T50 HID protocol.

use std::fs;
use std::path::{Path, PathBuf};

use base64::Engine;
use serde::Deserialize;
use supvan_proto::buffer::Density;
use supvan_proto::dither::DitherMode;
use supvan_proto::page;
use supvan_proto::printer::Printer;

#[derive(Deserialize, Default)]
struct Job {
    #[serde(rename = "devicePath", default)]
    device_path: String,
    #[serde(default)]
    copies: i32,
    #[serde(default)]
    labels: Vec<Label>,
}

#[derive(Deserialize, Default)]
struct Label {
    #[serde(default)]
    image: String,
}

fn find_devices() -> Vec<String> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir("/sys/class/hidraw") else {
        return out;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let uevent = entry.path().join("device/uevent");
        let Ok(text) = fs::read_to_string(&uevent) else {
            continue;
        };
        // HID_ID=0003:00001820:00002076  (bus:vendor:product)
        let is_supvan = text
            .lines()
            .find(|l| l.starts_with("HID_ID="))
            .map(|l| l.to_ascii_lowercase().contains(":00001820:"))
            .unwrap_or(false);
        if is_supvan {
            out.push(format!("/dev/{name}"));
        }
    }
    out.sort();
    out
}

fn pick_device(job_path: &str) -> (String, Vec<String>) {
    let devices = find_devices();
    let env = std::env::var("SUPVAN_PRINTER_DEVICE").unwrap_or_default();
    let chosen = if !job_path.trim().is_empty() {
        job_path.trim().to_string()
    } else if !env.trim().is_empty() {
        env.trim().to_string()
    } else {
        devices.first().cloned().unwrap_or_default()
    };
    (chosen, devices)
}

fn write_json(path: &str, value: &serde_json::Value) {
    if path.is_empty() {
        return;
    }
    let _ = fs::write(path, serde_json::to_string(value).unwrap_or_default());
}

async fn status(out_path: &str) -> serde_json::Value {
    let (device, devices) = pick_device("");
    if device.is_empty() {
        return serde_json::json!({
            "ok": false,
            "devices": [],
            "error": "未检测到硕方 T50/T80 打印机，请检查 USB 数据线和电源",
        });
    }
    match Printer::open_target(&device).await {
        Ok(printer) => {
            let st = printer.query_status().await.ok().flatten();
            let err = st.as_ref().and_then(|s| s.error_description());
            let busy = st
                .as_ref()
                .map(|s| s.printing || s.device_busy || s.buf_full)
                .unwrap_or(false);
            let ok = err.is_none() && !busy;
            let msg = err.unwrap_or_else(|| {
                if busy {
                    "打印机正在工作".to_string()
                } else {
                    String::new()
                }
            });
            serde_json::json!({
                "ok": ok,
                "devices": devices,
                "devicePath": device,
                "state": if ok { "Waiting" } else { "Busy" },
                "printDes": msg,
                "errorMsg": msg,
                "error": if ok { serde_json::Value::Null } else { serde_json::Value::String(msg) },
                "printedPages": 0,
                "totalPages": 0,
            })
        }
        Err(exc) => serde_json::json!({
            "ok": false,
            "devices": devices,
            "devicePath": device,
            "error": format!("无法打开打印机：{exc}"),
        }),
    }
}

async fn print_job(job_path: &str, out_path: &str) -> serde_json::Value {
    let text = match fs::read_to_string(job_path) {
        Ok(t) => t,
        Err(e) => {
            return serde_json::json!({"ok": false, "devices": [], "error": format!("读取任务失败：{e}")})
        }
    };
    let job: Job = match serde_json::from_str(&text) {
        Ok(j) => j,
        Err(e) => {
            return serde_json::json!({"ok": false, "devices": [], "error": format!("任务格式错误：{e}")})
        }
    };
    if job.labels.is_empty() {
        return serde_json::json!({"ok": false, "devices": [], "error": "没有要打印的标签"});
    }

    let (device, devices) = pick_device(&job.device_path);
    if device.is_empty() {
        return serde_json::json!({
            "ok": false,
            "devices": [],
            "error": "未检测到硕方 T50/T80 打印机，请检查 USB 数据线和电源",
        });
    }

    let printer = match Printer::open_target(&device).await {
        Ok(p) => p,
        Err(e) => {
            return serde_json::json!({
                "ok": false, "devices": devices, "devicePath": device,
                "error": format!("无法打开打印机：{e}")
            })
        }
    };
    if let Ok(Some(st)) = printer.query_status().await {
        if let Some(err) = st.error_description() {
            return serde_json::json!({
                "ok": false, "devices": devices, "devicePath": device,
                "error": err, "errorMsg": err,
            });
        }
    }

    let density = Density::uniform(4);
    let copies = if job.copies > 0 { job.copies } else { 1 };
    let mut printed = 0usize;
    let total = job.labels.len() * copies as usize;

    for _ in 0..copies {
        for label in &job.labels {
            if label.image.trim().is_empty() {
                continue;
            }
            let png = match base64::engine::general_purpose::STANDARD.decode(label.image.trim()) {
                Ok(b) => b,
                Err(e) => {
                    return serde_json::json!({
                        "ok": false, "devices": devices, "devicePath": device,
                        "error": format!("标签图片解码失败：{e}")
                    })
                }
            };
            let img = match image::load_from_memory_with_format(&png, image::ImageFormat::Png) {
                Ok(i) => i,
                Err(e) => {
                    return serde_json::json!({
                        "ok": false, "devices": devices, "devicePath": device,
                        "error": format!("标签图片无法解析：{e}")
                    })
                }
            };
            // 50 mm media is rendered 400 px wide, but the T50 head carries
            // 384 dots (48 mm). Fit the width to the head the way the vendor
            // SDK does instead of letting center_in_printhead crop 1 mm a side.
            let img = if img.width() > 384 {
                img.resize_exact(384, img.height(), image::imageops::FilterType::Nearest)
            } else {
                img
            };
            let gray = img.to_luma8();
            let (w, h) = (gray.width(), gray.height());
            let buffers = match page::grayscale(gray.as_raw(), w, h, density, DitherMode::Bayer) {
                Ok(b) => b,
                Err(e) => {
                    return serde_json::json!({
                        "ok": false, "devices": devices, "devicePath": device,
                        "error": format!("标签渲染失败：{e}")
                    })
                }
            };
            if let Err(e) = printer.print_buffers(&buffers).await {
                return serde_json::json!({
                    "ok": false, "devices": devices, "devicePath": device,
                    "error": format!("打印失败：{e}"),
                    "printedPages": printed,
                    "totalPages": total,
                });
            }
            printed += 1;
        }
    }

    serde_json::json!({
        "ok": true,
        "devices": devices,
        "devicePath": device,
        "state": "Waiting",
        "printedPages": printed,
        "totalPages": total,
    })
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let out_path = args.last().cloned().unwrap_or_default();
    if args.len() < 2 {
        write_json(
            &out_path,
            &serde_json::json!({"ok": false, "devices": [], "error": "usage: helper status <out> | print <job> <out>"}),
        );
        std::process::exit(2);
    }
    let command = args[1].to_ascii_lowercase();
    let result = match command.as_str() {
        "status" => status(&out_path).await,
        "print" if args.len() >= 3 => print_job(&args[2], &out_path).await,
        other => serde_json::json!({"ok": false, "devices": [], "error": format!("unknown command {other}")}),
    };
    write_json(&out_path, &result);
    if result["ok"].as_bool().unwrap_or(false) {
        std::process::exit(0);
    }
    std::process::exit(1);
}
