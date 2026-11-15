import socket
import threading
import time
import webbrowser

import uvicorn


def lan_ips():
    ips = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ":" not in ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def main():
    port = 8765
    if port_in_use(port):
        print(f"检测到 {port} 端口已有服务在运行，直接打开浏览器。")
        webbrowser.open(f"http://127.0.0.1:{port}")
        print("如果打不开，请先关闭之前启动的元件库管家窗口。")
        time.sleep(2)
        return
    ips = lan_ips()
    print("=" * 56)
    print("  元件库管家  PartVault")
    print("=" * 56)
    print(f"  本机访问:   http://127.0.0.1:{port}")
    for ip in ips:
        print(f"  其他设备访问: http://{ip}:{port}")
    print("  手机/平板打开上面的地址即可（同一 WiFi）")
    print("  关闭这个黑窗口即停止服务")
    print("=" * 56)
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
