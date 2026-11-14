import os
import sys
import socket
import hashlib
import time
import argparse
import urllib.request

DEFAULT_IP = "192.168.29.10"
DEFAULT_PORT = 3232

def find_firmware_bin():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)
    candidates = [
        os.path.join(root_dir, "firmware", "esp32-cam", ".pio", "build", "esp32cam", "firmware.bin"),
        os.path.join(script_dir, "firmware", "esp32-cam", ".pio", "build", "esp32cam", "firmware.bin"),
        os.path.join(script_dir, ".pio", "build", "esp32cam", "firmware.bin"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def get_routed_host_ip(target_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    finally:
        s.close()

def upload_ota(ip=DEFAULT_IP, port=DEFAULT_PORT, bin_path=None):
    if not bin_path:
        bin_path = find_firmware_bin()

    if not os.path.exists(bin_path):
        print(f"Error: Firmware binary not found at:\n  {bin_path}")
        print("Please build first: pio run -d firmware/esp32-cam")
        return False

    # Ensure clean RF guard-band XCLK (24 MHz) & QQVGA prior to OTA for maximum transfer speed
    try:
        urllib.request.urlopen(f"http://{ip}/control?var=xclk&val=24", timeout=2.5).read()
        urllib.request.urlopen(f"http://{ip}/control?var=framesize&val=0", timeout=2.5).read()
    except Exception:
        pass

    host_ip = get_routed_host_ip(ip)
    content_size = os.path.getsize(bin_path)
    with open(bin_path, 'rb') as f:
        file_md5 = hashlib.md5(f.read()).hexdigest()

    print(f"Target: {ip}:{port} (Host listener: {host_ip})")
    print(f"Firmware: {bin_path} ({content_size:,} bytes, MD5: {file_md5})")

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((host_ip, 0))
    server_sock.listen(1)
    host_port = server_sock.getsockname()[1]

    inv_msg = f"0 {host_port} {content_size} {file_md5}\n"
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.settimeout(3.0)

    print(f"Sending OTA invitation to {ip}:{port}...")
    for attempt in range(12):
        try:
            udp.sendto(inv_msg.encode(), (ip, port))
            resp = udp.recv(64).decode(errors='ignore')
            if "OK" in resp:
                print(f"ESP32 accepted invitation on attempt {attempt + 1}")
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("Error: No response from ESP32 on port 3232.")
        server_sock.close()
        udp.close()
        return False
    udp.close()

    server_sock.settimeout(18.0)
    try:
        conn, client_addr = server_sock.accept()
        print(f"ESP32 connected from {client_addr}")
    except Exception as e:
        print(f"Connection timeout from ESP32: {e}")
        server_sock.close()
        return False

    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(25.0)

    offset = 0
    t_start = time.time()
    last_log = t_start
    ota_ok = False

    print("Uploading firmware...")
    with open(bin_path, 'rb') as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            try:
                conn.sendall(chunk)
                res = conn.recv(32)
                if not res:
                    raise RuntimeError("Connection closed by ESP32")
                if b"OK" in res:
                    ota_ok = True
                offset += len(chunk)
            except Exception as e:
                print(f"\nTransfer error at {offset}/{content_size}: {e}")
                conn.close()
                server_sock.close()
                return False

            now = time.time()
            if now - last_log >= 2.0 or offset >= content_size:
                pct = (offset / content_size) * 100.0
                rate = (offset / 1024.0) / max(now - t_start, 0.001)
                bar = "#" * int(pct // 5) + "-" * (20 - int(pct // 5))
                print(f"  [{bar}] {pct:5.1f}% ({offset:,}/{content_size:,} bytes, {rate:.1f} KB/s)", flush=True)
                last_log = now

    if not ota_ok:
        try:
            conn.settimeout(30.0)
            while True:
                data = conn.recv(64)
                if not data:
                    break
                if b"OK" in data:
                    ota_ok = True
                    break
        except Exception:
            pass

    conn.close()
    server_sock.close()
    elapsed = time.time() - t_start

    if ota_ok:
        print(f"\n>>> OTA Upload Successful in {elapsed:.1f}s! ESP32 is rebooting. <<<")
        return True
    else:
        print(f"\nFlashing ended after {elapsed:.1f}s without final OK.")
        return False

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Upload ESP32 firmware over Wi-Fi (ArduinoOTA)")
    parser.add_argument("-i", "--ip", default=DEFAULT_IP, help=f"ESP32 IP (default: {DEFAULT_IP})")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"OTA Port (default: {DEFAULT_PORT})")
    parser.add_argument("-f", "--file", default=None, help="Path to firmware.bin")
    args = parser.parse_args()

    for attempt in range(1, 4):
        if attempt > 1:
            print(f"\nRetrying OTA (attempt {attempt}/3)...")
            time.sleep(2)
        if upload_ota(args.ip, args.port, args.file):
            sys.exit(0)

    sys.exit(1)
