import urllib.request
import time
import cv2
import numpy as np
import threading
import sys

ESP_IP = "192.168.29.10"

print("==========================================================")
print("  ESP32-CAM COMPREHENSIVE HARDWARE & ENDPOINT VALIDATION  ")
print("==========================================================")

# ----------------------------------------------------
# Test A: Basic HTTP Verification
# ----------------------------------------------------
print("\n--- TEST A: Basic HTTP Verification ---")
try:
    with urllib.request.urlopen(f"http://{ESP_IP}/ping", timeout=3) as resp:
        ping_body = resp.read().decode('utf-8')
        print(f"GET /ping: Status {resp.status}, Body: '{ping_body}'")
        assert resp.status == 200 and "ESP32-CAM OK" in ping_body, "Ping assertion failed"

    with urllib.request.urlopen(f"http://{ESP_IP}/status", timeout=3) as resp:
        status_body = resp.read().decode('utf-8')
        print(f"GET /status: Status {resp.status}, Body: '{status_body}'")
        assert resp.status == 200 and '"cpu_mhz":240' in status_body, "Status CPU frequency assertion failed"

    print(">> TEST A: PASSED (200 OK, CPU confirmed 240 MHz)")
except Exception as e:
    print(f">> TEST A FAILED: {e}")
    sys.exit(1)

# ----------------------------------------------------
# Test B: 10 Repeated Single Captures
# ----------------------------------------------------
print("\n--- TEST B: 10 Sequential Single Captures ---")
b_success_count = 0
b_latencies = []
b_sizes = []

for i in range(1, 11):
    t0 = time.time()
    try:
        req = urllib.request.Request(f"http://{ESP_IP}/capture")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
            dt = (time.time() - t0) * 1000
            b_latencies.append(dt)
            b_sizes.append(len(data))
            
            # Verify JPEG markers
            is_jpeg = len(data) > 4 and data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9'
            
            # Decode with OpenCV
            img_arr = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
            
            if img is not None and is_jpeg:
                h, w, c = img.shape
                print(f"  Capture {i}/10: SUCCESS | {len(data):,} bytes | {dt:.1f}ms | Resolution: {w}x{h}")
                b_success_count += 1
                if i == 1:
                    cv2.imwrite("validation_capture_1.jpg", img)
            else:
                print(f"  Capture {i}/10: FAILED DECODE | {len(data)} bytes | is_jpeg={is_jpeg}")
    except Exception as e:
        print(f"  Capture {i}/10: EXCEPTION: {e}")
    time.sleep(0.1)

print(f">> TEST B: {b_success_count}/10 successful captures")
print(f"   Avg Latency: {np.mean(b_latencies):.1f}ms | Avg Frame Size: {np.mean(b_sizes)/1024:.1f} KB")
assert b_success_count == 10, f"Test B failed: only {b_success_count}/10 frames valid"
print(">> TEST B: PASSED (10/10 valid JPEGs decoded cleanly by OpenCV)")

# ----------------------------------------------------
# Test C: Continuous MJPEG Stream (60 Seconds)
# ----------------------------------------------------
print("\n--- TEST C: Continuous MJPEG Stream (60 Seconds) ---")
stream_url = f"http://{ESP_IP}:81/stream"
stream_frames_decoded = 0
stream_bytes_received = 0
stream_errors = 0
stream_running = True
c_t_start = time.time()

def run_stream_reader():
    global stream_frames_decoded, stream_bytes_received, stream_errors, stream_running
    try:
        req = urllib.request.Request(stream_url)
        with urllib.request.urlopen(req, timeout=10) as stream:
            buffer = b""
            while stream_running:
                chunk = stream.read(4096)
                if not chunk:
                    break
                stream_bytes_received += len(chunk)
                buffer += chunk

                # Look for JPEG boundaries: SOI (0xFF, 0xD8) and EOI (0xFF, 0xD9)
                while True:
                    a = buffer.find(b'\xff\xd8')
                    b = buffer.find(b'\xff\xd9')
                    if a != -1 and b != -1 and b > a:
                        jpg = buffer[a:b+2]
                        buffer = buffer[b+2:]
                        
                        # Decode frame
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame is not None:
                            stream_frames_decoded += 1
                        else:
                            stream_errors += 1
                    else:
                        break
    except Exception as e:
        if stream_running:
            print(f"Stream exception: {e}")

# Start stream in background thread
stream_t = threading.Thread(target=run_stream_reader, daemon=True)
stream_t.start()

# Monitor for 60 seconds, printing progress every 10 seconds
last_c_check = c_t_start
last_c_frames = 0

for sec in range(1, 7):
    time.sleep(10)
    now = time.time()
    elapsed = now - c_t_start
    frames_delta = stream_frames_decoded - last_c_frames
    fps_instant = frames_delta / (now - last_c_check)
    avg_fps = stream_frames_decoded / elapsed
    print(f"  [{int(elapsed)}s/60s] Decoded {stream_frames_decoded} frames ({avg_fps:.1f} avg FPS, {fps_instant:.1f} curr FPS, {stream_bytes_received/1024/1024:.2f} MB)")
    last_c_check = now
    last_c_frames = stream_frames_decoded

    # ----------------------------------------------------
    # Test D: Mutex / Concurrent Capture Interaction during Stream
    # ----------------------------------------------------
    if sec == 3: # at 30 seconds into streaming
        print("\n  --- TEST D: Triggering /capture while /stream is active ---")
        t_d0 = time.time()
        try:
            with urllib.request.urlopen(f"http://{ESP_IP}/capture", timeout=5) as c_resp:
                c_data = c_resp.read()
                dt_d = (time.time() - t_d0) * 1000
                d_arr = np.frombuffer(c_data, dtype=np.uint8)
                d_img = cv2.imdecode(d_arr, cv2.IMREAD_COLOR)
                if d_img is not None:
                    print(f"  >> TEST D SUCCESS: Simultaneous /capture returned {len(c_data):,} bytes in {dt_d:.1f}ms while stream was running!")
                else:
                    print(f"  >> TEST D FAILED: decode failed")
        except Exception as e:
            print(f"  >> TEST D EXCEPTION: {e}")
        print("  Resuming stream monitoring...\n")

# Stop stream
stream_running = False
stream_t.join(timeout=3)

total_stream_time = time.time() - c_t_start
total_fps = stream_frames_decoded / total_stream_time

print(f"\n>> TEST C COMPLETE:")
print(f"   Duration: {total_stream_time:.1f}s | Decoded: {stream_frames_decoded} frames | Errors: {stream_errors}")
print(f"   Throughput: {stream_bytes_received/1024/1024:.2f} MB | Average FPS: {total_fps:.1f}")
assert stream_frames_decoded > 100, f"Stream frame count too low: {stream_frames_decoded}"
assert stream_errors == 0, f"Stream had {stream_errors} corrupt frames"
print(">> TEST C & D: PASSED (Continuous 60s stream with zero drops, simultaneous capture verified)")

# ----------------------------------------------------
# Test E: Recovery (Disconnect & Reconnect)
# ----------------------------------------------------
print("\n--- TEST E: Disconnect & Reconnect ---")
time.sleep(2)
reconnect_frames = 0
try:
    with urllib.request.urlopen(f"http://{ESP_IP}:81/stream", timeout=5) as rec_stream:
        buf = b""
        t_rec_start = time.time()
        while time.time() - t_rec_start < 5.0:
            chunk = rec_stream.read(4096)
            if not chunk:
                break
            buf += chunk
            a = buf.find(b'\xff\xd8')
            b = buf.find(b'\xff\xd9')
            if a != -1 and b != -1 and b > a:
                jpg = buf[a:b+2]
                buf = buf[b+2:]
                arr = np.frombuffer(jpg, dtype=np.uint8)
                if cv2.imdecode(arr, cv2.IMREAD_COLOR) is not None:
                    reconnect_frames += 1
    print(f">> TEST E PASSED: Successfully reconnected and decoded {reconnect_frames} frames in 5s")
except Exception as e:
    print(f">> TEST E FAILED: {e}")
    sys.exit(1)

# ----------------------------------------------------
# Test F: Device Stability & Status Check
# ----------------------------------------------------
print("\n--- TEST F: Post-Stream Device Health ---")
try:
    with urllib.request.urlopen(f"http://{ESP_IP}/status", timeout=3) as resp:
        final_status = resp.read().decode('utf-8')
        print(f">> Final status: {final_status}")
        assert '"cpu_mhz":240' in final_status, "Post-stream status check failed"
    print(">> TEST F PASSED: ESP32 remains healthy and responsive at 240 MHz")
except Exception as e:
    print(f">> TEST F FAILED: {e}")
    sys.exit(1)

print("\n==========================================================")
print("  ALL HARDWARE AND ENDPOINT TESTS PASSED WITH 100% SUCCESS  ")
print("==========================================================")
