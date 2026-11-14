import urllib.request
import time

print("Testing /capture...")
t0 = time.time()
try:
    req = urllib.request.Request("http://192.168.29.10/capture")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
        dt = time.time() - t0
        print(f"/capture SUCCESS: status={resp.status}, len={len(data)} in {dt:.2f}s")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        if len(data) > 2 and data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9':
            print("Valid JPEG markers found (SOI and EOI)!")
        else:
            print(f"Start: {data[:16].hex()}, End: {data[-16:].hex() if len(data)>=16 else ''}")
except Exception as e:
    dt = time.time() - t0
    print(f"/capture FAILED after {dt:.2f}s: {e}")
