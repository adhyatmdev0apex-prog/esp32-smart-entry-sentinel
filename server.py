"""
ESP32-CAM Smart Door Entry Detection & Facial Identity Server

Architecture:
- Detection: OpenCV YuNet (face_detection_yunet_2023mar.onnx, 5-point landmarks)
- Alignment: 2D Similarity Transform (cv2.FaceRecognizerSF.alignCrop, 112x112 canonical inner-face)
- Recognition: OpenCV SFace (face_recognition_sface_2021dec.onnx, 128-D L2-normalized hypersphere embedding)
  + Legacy dlib ResNet-34 128-D metric fallback compatibility
- Quality Gate: Laplacian sharpness, LAB-CLAHE illumination normalization, inter-ocular geometry,
  size & boundary checks to reject ears, extreme profiles, and motion blur
- Anti-Fragmentation Enrollment: Multi-frame candidate verification buffer + inconclusive gray-zone handling
- Identity Management: Side-by-side duplicate review, previewed transactional merge, and 3-tier safe deletion
"""

from flask import Flask, request, send_file, render_template, jsonify, Response, redirect, session
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import glob
import cv2
import numpy as np
import pickle
import json
import threading
import time
import shutil
import queue
import subprocess
import re
import urllib.request
import urllib.error
import socket
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from vlm_engine import vlm
from event_db import event_db
from notifications import notification_manager, NotificationEvent
from security_hardening import (
    security_guard,
    init_app_security,
    mask_notification_secrets,
    strip_masked_secrets,
    verify_secrets_h_login,
)
import atexit
try:
    import psutil
except ImportError:
    psutil = None

try:
    import face_recognition
    HAS_DLIB = True
except Exception:
    HAS_DLIB = False

app = Flask(__name__)
SERVER_START_EPOCH = time.time()
init_app_security(app, audit_cb=event_db.log_audit)

# ============================================================================
# CONFIGURATION & CALIBRATED THRESHOLDS
# ============================================================================
DATA_FOLDER = 'entrance_monitoring'
MODELS_FOLDER = 'models'
BACKUP_FOLDER = os.path.join(DATA_FOLDER, 'backups')
RUNTIME_DATA_DIR = 'data'
os.makedirs(RUNTIME_DATA_DIR, exist_ok=True)
ENCODINGS_FILE = os.path.join(RUNTIME_DATA_DIR, 'face_encodings.pkl')           # Legacy dlib 128-D encodings (kept in sync)
PROFILES_FILE = os.path.join(RUNTIME_DATA_DIR, 'identity_profiles.pkl')         # Structured SFace + metadata store
LABELS_FILE = os.path.join(RUNTIME_DATA_DIR, 'person_labels.json')              # Custom human-readable labels per person_id
DETECTIONS_FILE = os.path.join(RUNTIME_DATA_DIR, 'detections.json')
FULL_IMAGES_FOLDER = os.path.join(DATA_FOLDER, 'full_images')
FACES_FOLDER = os.path.join(DATA_FOLDER, 'faces')
UNRECOGNIZED_FOLDER = os.path.join(DATA_FOLDER, 'unrecognized')
TEMPLATES_FOLDER = 'templates'

MAX_FILES_PER_PERSON = 500
MAX_FULL_IMAGES = 100
MAX_ENCODINGS_PER_PERSON = 50
MIN_FACE_SIZE = 22                      # Identifies small distant faces down to 22-25 pixels
BACKUP_INTERVAL = 60
BUCKET_SIZE = 30

# Model & Threshold Configuration (InsightFace ResNet50 ArcFace buffalo_m 512-D)
MODEL_NAME = 'InsightFace ResNet50 ArcFace (512-D buffalo_m)'
FACE_MODEL_TAG = 'buffalo_m_r50'
YUNET_MODEL_PATH = os.path.join(MODELS_FOLDER, 'face_detection_yunet_2023mar.onnx')
SFACE_MODEL_PATH = os.path.join(MODELS_FOLDER, 'insightface_m', 'recognition', 'model.onnx')

DETECTION_CONF_THRESHOLD = 0.58       # Reject weak/blurry face detections early
ENROLL_DET_CONF_THRESHOLD = 0.72      # Stricter detection confidence required to enroll or update gallery
SFACE_MATCH_STRONG = 0.48             # Strong single/top-k cosine similarity match in 512-D
SFACE_MATCH_STANDARD = 0.42           # InsightFace calibrated cosine similarity threshold
SFACE_INCONCLUSIVE_LOW = 0.32         # Gray zone (0.32 - 0.42): inconclusive, never auto-assign or poison profile
DLIB_TOLERANCE = 0.48                 # Legacy dlib Euclidean distance threshold
MIN_QUALITY_FOR_MATCH = 26.0          # Reject unclear/blurry faces before attempting recognition
MIN_QUALITY_FOR_ENROLL = 40.0         # Minimum sample quality (0-100) to add to gallery or create new ID
CANDIDATE_FRAMES_REQUIRED = 3         # Consecutive/nearby high-quality frames required before creating new person_N
CANDIDATE_WINDOW_SECONDS = 15.0       # Time window for multi-frame candidate confirmation

# ESP32-CAM known MACs and IP candidates
ESP32_MACS = ["12-11-11-11-11-11", "84-1f-e8-68-c9-94"]
CANDIDATE_IPS = ["192.168.29.10", "192.168.29.3", "192.168.29.6"]

# Real-time connection and health tracking
camera_online = False
active_camera_ip = None
stream_fps = 0.0
last_frame_received_time = 0.0

# Camera hardware settings cache (OV2640 registers & onboard flash)
cached_camera_settings = {
    'framesize': 8,       # 8 = VGA (640x480)
    'quality': 12,        # 10 (highest quality) to 63 (lowest)
    'brightness': 0,      # -2 to 2
    'contrast': 0,        # -2 to 2
    'saturation': 0,      # -2 to 2
    'sharpness': 0,       # -2 to 2
    'denoise': 0,         # 0 or 1
    'special_effect': 0,  # 0: None, 1: Negative, 2: Grayscale, 3: Red, 4: Green, 5: Blue, 6: Sepia
    'flash': 0,           # 0 to 255 (PWM brightness on GPIO 4)
    'awb': 1,             # Auto White Balance (0/1)
    'wb_mode': 0,         # 0: Auto, 1: Sunny, 2: Cloudy, 3: Office, 4: Home
    'aec': 1,             # Auto Exposure Control (0/1)
    'aec2': 0,
    'ae_level': 0,        # -2 to 2
    'agc': 1,             # Auto Gain Control (0/1)
    'gainceiling': 0,
    'vflip': 0,           # Vertical flip (0/1)
    'hmirror': 0,         # Horizontal mirror (0/1)
    'online': False
}

# Create folders
for folder in [DATA_FOLDER, MODELS_FOLDER, BACKUP_FOLDER, FULL_IMAGES_FOLDER, FACES_FOLDER, UNRECOGNIZED_FOLDER, TEMPLATES_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Thread synchronization
state_lock = threading.RLock()
backup_lock = threading.Lock()
detector_lock = threading.RLock()

# Thread-safe queue for decoupling network ingestion from AI processing
frame_queue = queue.Queue(maxsize=1)
latest_jpeg = None
latest_jpeg_lock = threading.Lock()
latest_annotated_jpeg = None
show_face_overlay = True
recognition_enabled = True

# In-memory stores
known_face_encodings = {}   # {person_id: [dlib_vec, ...]} (legacy compatibility)
identity_profiles = {}      # {person_id: {'sface': [{'vec': np.ndarray, 'quality': float, 'det_score': float, 'ts': str, 'file': str}], 'centroid': np.ndarray, 'enrolled': bool}}
person_labels = {}          # {person_id: "Custom Name"}
person_detections = {}      # {person_id: [datetime, ...]}

# Temporary candidate tracks for unknown faces (prevents single-frame identity creation)
# Each item: {'id': str, 'vecs': [np.ndarray], 'first_seen': float, 'last_seen': float, 'best_crop': np.ndarray, 'best_full': np.ndarray, 'best_quality': float, 'best_det': float}
pending_candidate_tracks = []

# Live recognition telemetry log (most recent 25 events for dashboard inspection)
recent_recognition_events = deque(maxlen=25)
recognition_stats = {
    'recognized': 0,
    'inconclusive': 0,
    'unknown_pending': 0,
    'unusable_quality': 0,
    'no_face': 0,
    'last_status': 'Waiting for stream...',
    'last_event_time': None
}

stream_active = False
mjpeg_thread = None

# ============================================================================
# OFFLINE FRAME & NETWORK DISCOVERY
# ============================================================================
def create_offline_frame():
    """Generate high-contrast dark placeholder frame displayed when ESP-CAM is disconnected"""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (23, 15, 11)  # Deep slate dark background
    
    cv2.rectangle(img, (80, 110), (560, 370), (38, 27, 20), -1)
    cv2.rectangle(img, (80, 110), (560, 370), (80, 55, 55), 2)
    
    cv2.circle(img, (320, 180), 24, (60, 50, 225), -1)
    cv2.circle(img, (320, 180), 30, (90, 80, 255), 2)
    cv2.circle(img, (320, 180), 8, (255, 255, 255), -1)
    
    cv2.putText(img, "ESP-CAM OFFLINE", (175, 250), cv2.FONT_HERSHEY_DUPLEX, 1.0, (110, 110, 255), 2, cv2.LINE_AA)
    cv2.putText(img, "Camera is disconnected or rebooting", (155, 290), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (215, 215, 225), 1, cv2.LINE_AA)
    cv2.putText(img, "Auto-reconnecting as soon as power restores...", (125, 330), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (148, 163, 184), 1, cv2.LINE_AA)
    
    _, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return enc.tobytes()

OFFLINE_FRAME = create_offline_frame()

def create_sensor_error_frame():
    """Generate diagnostic frame when ESP32 board is on Wi-Fi but OV2640 sensor ribbon is unseated"""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = (23, 15, 11)  # Deep slate dark background
    
    cv2.rectangle(img, (60, 90), (580, 390), (38, 27, 20), -1)
    cv2.rectangle(img, (60, 90), (580, 390), (50, 160, 245), 2)
    
    # Amber warning circle
    cv2.circle(img, (320, 165), 32, (40, 180, 250), 2)
    cv2.putText(img, "!", (313, 178), cv2.FONT_HERSHEY_DUPLEX, 1.2, (40, 180, 250), 2, cv2.LINE_AA)
    
    cv2.putText(img, "ESP32-CAM ONLINE (PORT 81 CONNECTED)", (85, 230), cv2.FONT_HERSHEY_DUPLEX, 0.72, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(img, "Optical Sensor Hardware Error (0x105)", (135, 270), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (50, 180, 250), 1, cv2.LINE_AA)
    cv2.putText(img, "OV2640 camera ribbon cable is loose or unseated.", (115, 310), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (215, 215, 225), 1, cv2.LINE_AA)
    cv2.putText(img, "Please firmly re-seat the ribbon into the black connector.", (90, 345), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (148, 163, 184), 1, cv2.LINE_AA)
    
    _, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return enc.tobytes()

SENSOR_ERROR_FRAME = create_sensor_error_frame()

def find_esp32_ip_from_arp():
    """Dynamically find ESP32 IP from ARP table using known MAC addresses"""
    try:
        out = subprocess.check_output(['arp', '-a'], text=True, timeout=1.0)
        for mac in ESP32_MACS:
            t1 = mac.lower().replace(':', '-')
            t2 = mac.lower().replace('-', ':')
            for line in out.splitlines():
                clean = line.strip().lower()
                if t1 in clean or t2 in clean:
                    m = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return None

def get_candidate_stream_urls():
    """Retrieve list of candidate stream URLs to try"""
    urls = []
    arp_ip = find_esp32_ip_from_arp()
    if arp_ip:
        urls.append(f"http://{arp_ip}:81/stream")
    for ip in CANDIDATE_IPS:
        u = f"http://{ip}:81/stream"
        if u not in urls:
            urls.append(u)
    return urls

def get_current_camera_ip():
    """Return currently active camera IP or best candidate"""
    global active_camera_ip
    if active_camera_ip:
        return active_camera_ip
    arp_ip = find_esp32_ip_from_arp()
    if arp_ip:
        return arp_ip
    for ip in CANDIDATE_IPS:
        return ip
    return "192.168.29.10"

def calibrate_esp32_temperature(data):
    """
    Apply factory calibration offset to the raw ESP32-D0WD uncalibrated SAR ADC TSENS register.
    The raw uncalibrated register on ESP32-D0WD-V3 carries a +26.0°C hardware offset.
    Subtracting 26.0°C calibrates the reading to match true physical die temperature.
    """
    if 'temp_c' in data and data['temp_c'] is not None:
        try:
            raw_val = float(data['temp_c'])
            data['raw_temp_c'] = raw_val
            data['temp_c'] = round(max(20.0, raw_val - 26.0), 1)
        except Exception:
            pass
    return data

def refresh_camera_hardware_status():
    """Fetch live OV2640 hardware registers & internal chip temperature asynchronously"""
    cam_ip = active_camera_ip or (CANDIDATE_IPS[0] if CANDIDATE_IPS else '192.168.29.10')
    try:
        req = urllib.request.Request(f"http://{cam_ip}:80/status", headers={'User-Agent': 'ESP32-Dashboard'})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            if r.status == 200:
                data = json.loads(r.read().decode('utf-8'))
                data = calibrate_esp32_temperature(data)
                with state_lock:
                    cached_camera_settings.update(data)
                    cached_camera_settings['online'] = True
                return
    except Exception:
        pass
    with state_lock:
        cached_camera_settings['online'] = False

# ============================================================================
# DEEP FACE DETECTION & RECOGNITION ENGINE (YuNet + InsightFace MobileFaceNet)
# ============================================================================
yunet_detector = None
mobilefacenet_session = None
clahe_filter = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

REFERENCE_LANDMARKS_112 = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041]
], dtype=np.float32)

def ensure_models_downloaded():
    """Ensure YuNet and InsightFace MobileFaceNet ONNX models exist locally"""
    yunet_url = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'
    if not os.path.exists(YUNET_MODEL_PATH):
        print("Downloading YuNet face detector model...")
        urllib.request.urlretrieve(yunet_url, YUNET_MODEL_PATH)
    if not os.path.exists(SFACE_MODEL_PATH):
        print("Downloading InsightFace ResNet50 (buffalo_m) model from HF Hub...")
        hf_hub_download(repo_id='immich-app/buffalo_m', filename='recognition/model.onnx', local_dir=os.path.join(MODELS_FOLDER, 'insightface_m'))

def init_cv_models():
    """Initialize YuNet detector (small faces down to 36px) and InsightFace MobileFaceNet (512-D ArcFace)"""
    global yunet_detector, mobilefacenet_session
    ensure_models_downloaded()
    yunet_detector = cv2.FaceDetectorYN.create(
        YUNET_MODEL_PATH, "", (640, 480), DETECTION_CONF_THRESHOLD, 0.3, 5000
    )
    sess_opts = ort.SessionOptions()
    sess_opts.intra_op_num_threads = 2
    sess_opts.inter_op_num_threads = 1
    sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    from npu import get_execution_providers
    providers, _accel_target = get_execution_providers(ort.get_available_providers(), use_gpu=True, use_npu=True)
    mobilefacenet_session = ort.InferenceSession(SFACE_MODEL_PATH, sess_options=sess_opts, providers=providers)
    print(f"[*] Initialized YuNet + InsightFace MobileFaceNet (512-D ArcFace) on {mobilefacenet_session.get_providers()}")

def extract_mobilefacenet_feature(bgr_image, landmarks_or_row):
    """Align face to 112x112 via 5 similarity landmarks and extract 512-D ArcFace vector"""
    if len(landmarks_or_row) >= 15:
        lms = np.array([
            [landmarks_or_row[4], landmarks_or_row[5]],
            [landmarks_or_row[6], landmarks_or_row[7]],
            [landmarks_or_row[8], landmarks_or_row[9]],
            [landmarks_or_row[10], landmarks_or_row[11]],
            [landmarks_or_row[12], landmarks_or_row[13]]
        ], dtype=np.float32)
    else:
        lms = np.array(landmarks_or_row, dtype=np.float32)

    tform, _ = cv2.estimateAffinePartial2D(lms, REFERENCE_LANDMARKS_112, method=cv2.LMEDS)
    if tform is None:
        aligned = cv2.resize(bgr_image, (112, 112))
    else:
        aligned = cv2.warpAffine(bgr_image, tform, (112, 112), borderValue=0.0)

    blob = cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB)
    blob = ((blob.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)
    blob = np.expand_dims(blob, axis=0)

    with detector_lock:
        raw_feat = mobilefacenet_session.run(None, {'input.1': blob})[0][0]
    return normalize_vec(raw_feat)

def apply_illumination_normalization(bgr_img):
    """Normalize uneven doorway lighting using LAB-space CLAHE on luminance channel"""
    gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)
    mean_lum = float(np.mean(gray))
    # Apply stronger CLAHE when image is dim or high-contrast backlit
    if mean_lum < 95.0 or np.std(gray) > 65.0:
        lab = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = clahe_filter.apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    return bgr_img

def normalize_vec(v):
    """L2-normalize a 128-D embedding vector"""
    arr = np.asarray(v, dtype=np.float32).flatten()
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 1e-8 else arr

def cosine_sim(v1, v2):
    """Compute cosine similarity between two L2-normalized vectors (-1.0 to 1.0)"""
    a = normalize_vec(v1)
    b = normalize_vec(v2)
    return float(np.dot(a, b))

def recompute_profile_centroid(person_id):
    """Update the quality-weighted L2-normalized centroid for a person profile"""
    prof = identity_profiles.get(person_id)
    if not prof or not prof.get('sface'):
        if prof is not None:
            prof['centroid'] = None
        return
    samples = prof['sface']
    vecs = []
    weights = []
    for s in samples:
        vecs.append(normalize_vec(s['vec']))
        weights.append(max(10.0, float(s.get('quality', 50.0))))
    weights = np.array(weights, dtype=np.float32)
    weights /= np.sum(weights)
    weighted_sum = np.sum([v * w for v, w in zip(vecs, weights)], axis=0)
    prof['centroid'] = normalize_vec(weighted_sum)

def assess_face_quality(bgr_image, face_row):
    """
    Evaluate whether a detected face is suitable for recognition and/or enrollment.
    Checks:
    1. Detection score
    2. Bounding box dimensions & frame margin
    3. 5-keypoint anatomical symmetry (rejects ears, back of head, extreme >55 deg yaw)
    4. Sharpness (Laplacian + Sobel gradient energy on inner face)
    5. Exposure (rejects pitch-black or blown-out white crops)
    Returns: (is_usable: bool, quality_score: float 0-100, reason: str, bbox: tuple(top, right, bottom, left))
    """
    img_h, img_w = bgr_image.shape[:2]
    x, y, w, h = float(face_row[0]), float(face_row[1]), float(face_row[2]), float(face_row[3])
    det_score = float(face_row[14])

    left = max(0, int(round(x)))
    top = max(0, int(round(y)))
    right = min(img_w, int(round(x + w)))
    bottom = min(img_h, int(round(y + h)))
    bw = right - left
    bh = bottom - top

    if det_score < DETECTION_CONF_THRESHOLD:
        return False, 0.0, f"Low detection confidence ({det_score:.2f})", (top, right, bottom, left)

    if bw < 32 or bh < 36 or max(bw, bh) < 40:
        return False, 10.0, f"Face too small ({bw}x{bh}px)", (top, right, bottom, left)

    # Landmark coordinates: right_eye(4,5), left_eye(6,7), nose(8,9), right_mouth(10,11), left_mouth(12,13)
    re_x, re_y = float(face_row[4]), float(face_row[5])
    le_x, le_y = float(face_row[6]), float(face_row[7])
    nose_x, nose_y = float(face_row[8]), float(face_row[9])
    rm_x, rm_y = float(face_row[10]), float(face_row[11])
    lm_x, lm_y = float(face_row[12]), float(face_row[13])

    eye_dist = float(np.hypot(le_x - re_x, le_y - re_y))
    eye_ratio = eye_dist / max(1.0, float(bw))

    # Reject false positives (like ears) or extreme profile views where eyes collapse together
    # Relaxed for distant/small faces where eye distance in pixels is naturally small
    if eye_ratio < 0.12:
        return False, 15.0, f"Extreme head yaw or non-face ({eye_ratio:.2f} eye ratio)", (top, right, bottom, left)

    # Check vertical anatomical ordering: eyes should be above mouth
    eye_mid_y = 0.5 * (re_y + le_y)
    mouth_mid_y = 0.5 * (rm_y + lm_y)
    if mouth_mid_y <= eye_mid_y + (0.05 * bh):
        return False, 15.0, "Invalid facial landmark geometry", (top, right, bottom, left)

    # Estimate yaw symmetry (nose horizontal position relative to eyes)
    eye_mid_x = 0.5 * (re_x + le_x)
    yaw_asymmetry = abs(nose_x - eye_mid_x) / max(1.0, eye_dist)
    max_yaw = 0.95 if bw < 55 else 0.85
    if yaw_asymmetry > max_yaw:
        return False, 20.0, f"Extreme side profile (asymmetry {yaw_asymmetry:.2f})", (top, right, bottom, left)

    crop = bgr_image[top:bottom, left:right]
    if crop.size == 0:
        return False, 0.0, "Empty face crop", (top, right, bottom, left)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < 15.0 or brightness > 245.0:
        return False, 18.0, f"Poor exposure (brightness {brightness:.0f})", (top, right, bottom, left)

    # Sharpness metric (Laplacian variance + Tenengrad Sobel gradient)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    sobel_energy = float(np.mean(gx * gx + gy * gy))

    if lap_var < 7.0 and sobel_energy < 110.0:
        return False, 15.0, f"Motion blur (sharpness {lap_var:.1f})", (top, right, bottom, left)

    # Compute composite 0-100 quality score
    sharp_score = min(35.0, (lap_var / 20.0) * 20.0 + (sobel_energy / 400.0) * 15.0)
    pose_score = max(0.0, 25.0 * (1.0 - min(1.0, yaw_asymmetry / 0.85)))
    size_score = min(20.0, (max(bw, bh) / 60.0) * 20.0)
    conf_score = min(20.0, ((det_score - 0.45) / 0.55) * 20.0)
    quality = round(float(sharp_score + pose_score + size_score + conf_score), 1)

    if quality < MIN_QUALITY_FOR_MATCH:
        return False, quality, f"Low overall face quality ({quality:.1f}/100)", (top, right, bottom, left)

    return True, quality, "OK", (top, right, bottom, left)

def detect_and_extract_faces(bgr_image):
    """
    Run YuNet detection down to 36px + quality assessment + InsightFace MobileFaceNet 512-D ArcFace embedding.
    Returns list of dicts:
    {
      'usable': bool,
      'quality': float,
      'reason': str,
      'det_score': float,
      'bbox': (top, right, bottom, left),
      'sface_vec': np.ndarray(512,) or None,
      'face_row': np.ndarray
    }
    """
    norm_img = apply_illumination_normalization(bgr_image)
    h, w = norm_img.shape[:2]

    with detector_lock:
        yunet_detector.setInputSize((w, h))
        _, faces = yunet_detector.detect(norm_img)

        if faces is None or len(faces) == 0:
            return []

        results = []
        for face_row in faces:
            usable, quality, reason, bbox = assess_face_quality(bgr_image, face_row)
            sface_vec = None
            if usable:
                sface_vec = extract_mobilefacenet_feature(norm_img, face_row)
            results.append({
                'usable': usable,
                'quality': quality,
                'reason': reason,
                'det_score': float(face_row[14]),
                'bbox': bbox,
                'sface_vec': sface_vec,
                'face_row': face_row
            })
        return results

# ============================================================================
# PERSISTENCE & STARTUP BACKFILL
# ============================================================================
def load_encodings():
    if os.path.exists(ENCODINGS_FILE):
        try:
            with open(ENCODINGS_FILE, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Warning loading {ENCODINGS_FILE}: {e}")
    return {}

def save_encodings(encodings=None):
    with state_lock:
        target = encodings if encodings is not None else known_face_encodings
        with open(ENCODINGS_FILE, 'wb') as f:
            pickle.dump(target, f)

def load_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Warning loading {PROFILES_FILE}: {e}")
    return {}

def save_profiles():
    with state_lock:
        with open(PROFILES_FILE, 'wb') as f:
            pickle.dump(identity_profiles, f)

def load_labels():
    if os.path.exists(LABELS_FILE):
        try:
            with open(LABELS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_labels():
    with state_lock:
        with open(LABELS_FILE, 'w', encoding='utf-8') as f:
            json.dump(person_labels, f, indent=2)

def load_detections():
    if os.path.exists(DETECTIONS_FILE):
        try:
            with open(DETECTIONS_FILE, 'r') as f:
                data = json.load(f)
                result = {}
                for person_id, timestamps in data.items():
                    result[person_id] = [datetime.fromisoformat(ts) for ts in timestamps]
                return result
        except Exception as e:
            print(f"Warning loading {DETECTIONS_FILE}: {e}")
    return {}

def save_detections():
    with backup_lock:
        with state_lock:
            data = {
                person_id: [ts.isoformat() for ts in timestamps]
                for person_id, timestamps in person_detections.items()
            }
        with open(DETECTIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)

def extract_sface_for_saved_image(crop_path):
    """Extract SFace embedding and realistic quality metric from a saved face crop"""
    crop_im = cv2.imread(crop_path)
    if crop_im is None:
        return None, 0.0, 0.0

    # Measure true sharpness and exposure on the actual saved face crop
    crop_gray = cv2.cvtColor(crop_im, cv2.COLOR_BGR2GRAY)
    crop_lap = float(cv2.Laplacian(crop_gray, cv2.CV_64F).var())
    crop_bright = float(np.mean(crop_gray))

    bname = os.path.basename(crop_path)
    ts_prefix = bname.split('_face')[0]
    full_path = os.path.join(FULL_IMAGES_FOLDER, ts_prefix + '.jpg')
    # Only fall back to full_path if crop is a real face crop (not the 142x144 ear false-positive in person_7)
    search_paths = [crop_path]
    if os.path.exists(full_path) and crop_im.shape[0] >= 160 and crop_im.shape[1] >= 160:
        search_paths.append(full_path)

    with detector_lock:
        for path in search_paths:
            im = cv2.imread(path)
            if im is None:
                continue
            norm_im = apply_illumination_normalization(im)
            for rot in [None, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_90_CLOCKWISE]:
                test_im = cv2.rotate(norm_im, rot) if rot is not None else norm_im
                padded = cv2.copyMakeBorder(test_im, 36, 36, 36, 36, cv2.BORDER_REPLICATE)
                yunet_detector.setInputSize((padded.shape[1], padded.shape[0]))
                _, faces = yunet_detector.detect(padded)
                if faces is not None and len(faces) > 0:
                    best_face = faces[np.argmax(faces[:, -1])]
                    det_conf = float(best_face[-1])
                    if det_conf >= 0.45:
                        feat = extract_mobilefacenet_feature(padded, best_face)
                        sharp_part = min(45.0, (crop_lap / 35.0) * 45.0)
                        exp_part = 20.0 if 35.0 <= crop_bright <= 210.0 else 10.0
                        conf_part = min(30.0, ((det_conf - 0.40) / 0.60) * 30.0)
                        q = round(max(15.0, min(96.0, sharp_part + exp_part + conf_part)), 1)
                        return feat, q, det_conf
        # Fallback: if landmark detector missed the tight crop, extract directly from resized crop
        aligned = cv2.resize(crop_im, (112, 112))
        blob = cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB)
        blob = ((blob.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)
        blob = np.expand_dims(blob, axis=0)
        with detector_lock:
            raw_feat = mobilefacenet_session.run(None, {'input.1': blob})[0][0]
        return normalize_vec(raw_feat), 65.0, 0.70
    return None, 0.0, 0.0

def sync_and_backfill_profiles():
    """Synchronize disk person folders with identity_profiles and backfill SFace embeddings"""
    with state_lock:
        person_folders = sorted(glob.glob(os.path.join(FACES_FOLDER, 'person_*')))
        updated = False
        for folder in person_folders:
            pid = os.path.basename(folder)
            if pid not in identity_profiles:
                identity_profiles[pid] = {
                    'sface': [],
                    'centroid': None,
                    'enrolled': True,
                    'created_at': datetime.now().isoformat()
                }
                updated = True

            prof = identity_profiles[pid]
            # Prune any orphaned gallery entries whose image was removed from disk
            if prof.get('sface'):
                orig_len = len(prof['sface'])
                prof['sface'] = [s for s in prof['sface'] if os.path.exists(os.path.join(folder, s.get('file', '')))]
                if len(prof['sface']) != orig_len:
                    recompute_profile_centroid(pid)
                    updated = True
                    print(f"[*] Cleaned up {orig_len - len(prof['sface'])} orphaned gallery embeddings for {pid}")

            if (not prof.get('sface') or prof.get('model') != FACE_MODEL_TAG) and prof.get('enrolled', True):
                prof['sface'] = []
                img_files = sorted(glob.glob(os.path.join(folder, '*.jpg')), key=os.path.getmtime, reverse=True)
                # Sample up to 15 images to build a clean ArcFace gallery
                for img_path in img_files[:15]:
                    feat, q, dconf = extract_sface_for_saved_image(img_path)
                    if feat is not None:
                        prof['sface'].append({
                            'vec': feat,
                            'quality': q,
                            'det_score': dconf,
                            'ts': datetime.fromtimestamp(os.path.getmtime(img_path)).isoformat(),
                            'file': os.path.basename(img_path)
                        })
                prof['model'] = FACE_MODEL_TAG
                recompute_profile_centroid(pid)
                updated = True
                print(f"[*] Re-indexed {len(prof['sface'])} {FACE_MODEL_TAG} embeddings for {pid}")
            elif prof.get('centroid') is None and prof.get('sface'):
                recompute_profile_centroid(pid)
                updated = True

        if updated:
            save_profiles()

# ============================================================================
# IDENTITY MATCHING & ANTI-FRAGMENTATION ENROLLMENT
# ============================================================================
def match_face_against_enrolled(sface_vec):
    """
    Compare a 128-D SFace embedding against all enrolled person profiles.
    Uses Top-3 gallery similarity + Centroid similarity (never penalizes pose diversity!).
    Returns:
      (status: 'matched'|'inconclusive'|'unknown', person_id: str|None, similarity: float, details: dict)
    """
    with state_lock:
        if not identity_profiles:
            return 'unknown', None, 0.0, {}

        scores_by_person = []
        for pid, prof in identity_profiles.items():
            if not prof.get('enrolled', True):
                continue
            samples = prof.get('sface', [])
            if not samples:
                continue

            sims = sorted([cosine_sim(sface_vec, s['vec']) for s in samples], reverse=True)
            max_sim = sims[0]
            top_k = sims[:min(3, len(sims))]
            topk_mean = float(np.mean(top_k))
            centroid_sim = cosine_sim(sface_vec, prof['centroid']) if prof.get('centroid') is not None else topk_mean

            # Composite similarity: rewards strong view match (max_sim/topk) + overall identity centroid
            combined_sim = 0.50 * max_sim + 0.30 * topk_mean + 0.20 * centroid_sim
            scores_by_person.append((combined_sim, max_sim, centroid_sim, pid))

        if not scores_by_person:
            return 'unknown', None, 0.0, {}

        scores_by_person.sort(key=lambda x: x[0], reverse=True)
        best_combined, best_max, best_centroid, best_pid = scores_by_person[0]
        second_max = scores_by_person[1][1] if len(scores_by_person) > 1 else 0.0
        margin = best_max - second_max

        details = {
            'best_person': best_pid,
            'max_sim': round(best_max, 3),
            'centroid_sim': round(best_centroid, 3),
            'combined_sim': round(best_combined, 3),
            'second_max_sim': round(second_max, 3),
            'margin': round(margin, 3)
        }

        # Tier 1: High-confidence direct match with centroid + margin guard
        if best_max >= SFACE_MATCH_STRONG and best_centroid >= 0.32 and (margin >= 0.04 or best_max >= 0.58 or len(scores_by_person) == 1):
            return 'matched', best_pid, best_max, details

        # Tier 2: Standard threshold match with strict margin + centroid confirmation
        if best_max >= SFACE_MATCH_STANDARD and best_centroid >= 0.34 and (margin >= 0.06 or len(scores_by_person) == 1):
            return 'matched', best_pid, best_max, details

        # Tier 3: Inconclusive / Ambiguous gray zone (do NOT create a duplicate profile or mis-assign!)
        if best_max >= SFACE_INCONCLUSIVE_LOW:
            return 'inconclusive', best_pid, best_max, details

        return 'unknown', None, best_max, details

def add_sface_sample_smart(person_id, sface_vec, quality, det_score, filename):
    """
    Curate multiple embeddings per person:
    - Reject low-quality samples (< MIN_QUALITY_FOR_ENROLL)
    - Reject anomalous outliers that don't fit the profile (< SFACE_MATCH_STANDARD)
    - Avoid redundant near-identical duplicates (cosine_sim > 0.92) once gallery has >= 5 samples
    - Replace lowest-quality sample if gallery is full
    """
    if quality < MIN_QUALITY_FOR_ENROLL or det_score < ENROLL_DET_CONF_THRESHOLD:
        return False

    with state_lock:
        if person_id not in identity_profiles:
            identity_profiles[person_id] = {
                'sface': [],
                'centroid': None,
                'enrolled': True,
                'created_at': datetime.now().isoformat()
            }
        prof = identity_profiles[person_id]
        samples = prof['sface']

        new_entry = {
            'vec': normalize_vec(sface_vec),
            'quality': float(quality),
            'det_score': float(det_score),
            'ts': datetime.now().isoformat(),
            'file': filename
        }

        if len(samples) == 0:
            samples.append(new_entry)
            recompute_profile_centroid(person_id)
            return True

        sims = [cosine_sim(sface_vec, s['vec']) for s in samples]
        max_sim = max(sims)
        centroid_sim = cosine_sim(sface_vec, prof['centroid']) if prof.get('centroid') is not None else max_sim

        # Protect profile against single bad or borderline detection corruption
        if max_sim < SFACE_MATCH_STRONG or centroid_sim < 0.35:
            return False

        # Always collect first 4 diverse samples if not nearly identical
        if len(samples) < 4 and max_sim < 0.94:
            samples.append(new_entry)
            recompute_profile_centroid(person_id)
            return True

        # Add new pose/lighting variation if moderately novel (0.40 <= max_sim <= 0.88)
        if max_sim <= 0.88:
            if len(samples) < MAX_ENCODINGS_PER_PERSON:
                samples.append(new_entry)
                recompute_profile_centroid(person_id)
                return True
            else:
                # Replace the lowest-quality existing sample if this sample is higher quality
                min_q_idx = int(np.argmin([s.get('quality', 0.0) for s in samples]))
                if quality > samples[min_q_idx].get('quality', 0.0) + 3.0:
                    samples[min_q_idx] = new_entry
                    recompute_profile_centroid(person_id)
                    return True
        elif max_sim > 0.88:
            # If very similar to an existing sample but significantly sharper, upgrade that sample
            best_idx = int(np.argmax(sims))
            if quality > samples[best_idx].get('quality', 0.0) + 8.0:
                samples[best_idx] = new_entry
                recompute_profile_centroid(person_id)
                return True

        return False

def handle_unknown_candidate(sface_vec, quality, det_score, face_crop, full_image, timestamp):
    """
    Controlled multi-frame enrollment buffer:
    - Requires a clear, high-quality face (quality >= MIN_QUALITY_FOR_ENROLL, det_score >= ENROLL_DET_CONF_THRESHOLD)
    - Tracks candidate across nearby frames within CANDIDATE_WINDOW_SECONDS
    - Only promotes to a permanent person_N identity after CANDIDATE_FRAMES_REQUIRED consistent sightings
    """
    now_ts = time.time()
    with state_lock:
        # Prune expired candidate tracks
        expired = [t for t in pending_candidate_tracks if (now_ts - t['last_seen']) > CANDIDATE_WINDOW_SECONDS]
        for t in expired:
            pending_candidate_tracks.remove(t)

        if quality < MIN_QUALITY_FOR_ENROLL or det_score < ENROLL_DET_CONF_THRESHOLD:
            return None, 'unknown_low_quality_for_enroll', 0

        # Match against active pending tracks
        best_track = None
        best_sim = 0.0
        for track in pending_candidate_tracks:
            sims = [cosine_sim(sface_vec, v) for v in track['vecs']]
            msim = max(sims)
            if msim > best_sim:
                best_sim = msim
                best_track = track

        if best_track is not None and best_sim >= 0.52:
            best_track['vecs'].append(normalize_vec(sface_vec))
            best_track['last_seen'] = now_ts
            if quality >= best_track['best_quality']:
                best_track['best_quality'] = quality
                best_track['best_det'] = det_score
                best_track['best_crop'] = face_crop.copy()
                best_track['best_full'] = full_image.copy()

            sightings = len(best_track['vecs'])
            if sightings >= CANDIDATE_FRAMES_REQUIRED:
                # Re-verify aggregated candidate centroid against enrolled identities before creating new ID
                cand_centroid = normalize_vec(np.mean(best_track['vecs'], axis=0))
                status, matched_pid, sim, _ = match_face_against_enrolled(cand_centroid)
                pending_candidate_tracks.remove(best_track)
                if status in ('matched', 'inconclusive') and matched_pid is not None:
                    return matched_pid, 'matched_after_buffer', sightings

                # Promote to a new permanent identity
                new_num = get_next_person_id()
                person_id = f'person_{new_num}'
                person_folder = os.path.join(FACES_FOLDER, person_id)
                os.makedirs(person_folder, exist_ok=True)
                identity_profiles[person_id] = {
                    'sface': [],
                    'centroid': None,
                    'enrolled': True,
                    'created_at': timestamp.isoformat()
                }
                return person_id, 'newly_enrolled', sightings
            else:
                return None, 'unknown_pending', sightings
        else:
            # Start a new candidate track
            pending_candidate_tracks.append({
                'id': f"cand_{int(now_ts*1000)}",
                'vecs': [normalize_vec(sface_vec)],
                'first_seen': now_ts,
                'last_seen': now_ts,
                'best_crop': face_crop.copy(),
                'best_full': full_image.copy(),
                'best_quality': quality,
                'best_det': det_score
            })
            return None, 'unknown_pending', 1

def get_next_person_id():
    """Generate next sequential person ID across folders and profile keys"""
    with state_lock:
        person_folders = glob.glob(os.path.join(FACES_FOLDER, 'person_*'))
        ids = []
        for folder in person_folders:
            try:
                ids.append(int(os.path.basename(folder).split('_')[1]))
            except Exception:
                pass
        for pid in identity_profiles.keys():
            try:
                ids.append(int(pid.split('_')[1]))
            except Exception:
                pass
        return max(ids) + 1 if ids else 1

# ============================================================================
# STREAM INGESTION & FRAME PROCESSING
# ============================================================================
def record_recognition_event(status, person_id=None, similarity=0.0, quality=0.0, reason="", thumb_file=None):
    now_str = datetime.now().strftime('%H:%M:%S')
    with state_lock:
        if status in recognition_stats:
            recognition_stats[status] += 1
        label = person_labels.get(person_id) if person_id else None
        display_who = f"{label} ({person_id})" if label else (person_id or "Unknown")
        if status == 'recognized':
            recognition_stats['last_status'] = f"Recognized {display_who} ({similarity*100:.1f}% sim, Q:{quality:.0f})"
        elif status == 'inconclusive':
            recognition_stats['last_status'] = f"Inconclusive near {display_who} ({similarity*100:.1f}% sim) — skipped"
        elif status == 'unknown_pending':
            recognition_stats['last_status'] = f"Verifying unknown face ({reason}, Q:{quality:.0f})"
        elif status == 'unusable_quality':
            recognition_stats['last_status'] = f"Rejected low-quality detection: {reason}"
        recognition_stats['last_event_time'] = now_str

        if status != 'no_face':
            recent_recognition_events.appendleft({
                'time': now_str,
                'status': status,
                'person_id': person_id,
                'person_label': label,
                'similarity': round(float(similarity), 3),
                'quality': round(float(quality), 1),
                'reason': reason,
                'thumb': thumb_file
            })

    if status != 'no_face':
        try:
            objs = list(vlm.latest_objects) if getattr(vlm, 'enabled', False) else None
            event_db.log_recognition_event(
                status,
                person_id=person_id,
                person_label=label,
                similarity=similarity,
                quality=quality,
                reason=reason,
                thumb=thumb_file,
                objects=objs,
            )
        except Exception as e:
            print(f"[!] EventDB log error: {e}")

        # Phase 10: Emit notification events (non-blocking queue)
        try:
            snap_bytes = None
            with latest_jpeg_lock:
                snap_bytes = latest_jpeg
            if status == 'recognized' and label:
                notification_manager.emit(NotificationEvent(
                    event_type='KNOWN_PERSON',
                    title=f"Known Person: {label}",
                    message=f"{label} ({person_id}) recognized with {similarity*100:.1f}% confidence (Q:{quality:.0f}).",
                    severity='info',
                    person_id=person_id,
                    person_label=label,
                    image_bytes=snap_bytes,
                    metadata={'similarity': round(float(similarity), 3), 'quality': round(float(quality), 1), 'thumb': thumb_file},
                    dedup_key=f"KNOWN_PERSON:{person_id}",
                ))
            elif status == 'recognized' and not label:
                notification_manager.emit(NotificationEvent(
                    event_type='UNKNOWN_PERSON',
                    title=f"Unlabeled Visitor ({person_id})",
                    message=f"Unlabeled person {person_id} detected at door (Q:{quality:.0f}).",
                    severity='warning',
                    person_id=person_id,
                    image_bytes=snap_bytes,
                    metadata={'similarity': round(float(similarity), 3), 'quality': round(float(quality), 1), 'thumb': thumb_file},
                    dedup_key=f"UNKNOWN_PERSON:{person_id}",
                ))
            elif status == 'unknown_pending':
                notification_manager.emit(NotificationEvent(
                    event_type='UNKNOWN_PERSON',
                    title="Unknown Person Detected",
                    message=f"Unrecognized face at door ({reason}, Q:{quality:.0f}).",
                    severity='warning',
                    image_bytes=snap_bytes,
                    metadata={'quality': round(float(quality), 1), 'reason': reason, 'thumb': thumb_file},
                    dedup_key="UNKNOWN_PERSON:pending",
                ))
        except Exception as e:
            print(f"[!] Notification emit error: {e}")

def render_face_overlay(bgr_image, face_boxes):
    """
    Renders high-visibility cyber-style bounding boxes and name/quality badges directly onto frame.
    face_boxes: list of dicts with:
      'bbox': (top, right, bottom, left)
      'label': str (e.g. 'Adhyatm (87%)')
      'color': (B, G, R) tuple (e.g. Green (46, 204, 113) or Yellow (0, 215, 255))
      'status': 'recognized' | 'inconclusive' | 'unknown_pending' | 'unusable_quality'
    """
    canvas = bgr_image.copy()
    h, w = canvas.shape[:2]

    for item in face_boxes:
        top, right, bottom, left = item['bbox']
        color = item['color']
        label = item['label']

        left = max(0, min(w - 1, left))
        right = max(0, min(w - 1, right))
        top = max(0, min(h - 1, top))
        bottom = max(0, min(h - 1, bottom))
        bw = right - left
        bh = bottom - top

        if bw < 5 or bh < 5:
            continue

        # 1. Main bounding box
        cv2.rectangle(canvas, (left, top), (right, bottom), color, 2, cv2.LINE_AA)

        # 2. Sleek cyber corner accents
        c_len = max(6, min(20, int(min(bw, bh) * 0.22)))
        cv2.line(canvas, (left, top), (left + c_len, top), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (left, top), (left, top + c_len), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (right, top), (right - c_len, top), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (right, top), (right, top + c_len), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (left, bottom), (left + c_len, bottom), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (left, bottom), (left, bottom - c_len), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (right, bottom), (right - c_len, bottom), color, 3, cv2.LINE_AA)
        cv2.line(canvas, (right, bottom), (right, bottom - c_len), color, 3, cv2.LINE_AA)

        # 3. Label badge
        font = cv2.FONT_HERSHEY_DUPLEX
        font_scale = 0.42
        thickness = 1
        (tw, th), baseline = cv2.getTextSize(label, font, font_scale, thickness)

        badge_h = th + 8
        badge_w = tw + 10

        if top - badge_h - 2 >= 0:
            by1 = top - badge_h - 2
            by2 = top - 2
        else:
            by1 = top + 2
            by2 = top + badge_h + 2

        bx1 = max(0, left)
        bx2 = min(w - 1, bx1 + badge_w)

        # Dark high-contrast pill backdrop
        cv2.rectangle(canvas, (bx1, by1), (bx2, by2), (20, 20, 25), cv2.FILLED)
        # Colored top stripe on the badge
        cv2.rectangle(canvas, (bx1, by1), (bx2, by1 + 2), color, cv2.FILLED)
        # Text
        cv2.putText(canvas, label, (bx1 + 5, by1 + th + 4), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)

    return canvas

def process_frame(jpg_bytes):
    """Process a single validated JPEG frame through DirectML Object Detection + YuNet/SFace"""
    global latest_annotated_jpeg
    try:
        nparr = np.frombuffer(jpg_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            return

        timestamp = datetime.now()
        obj_overlay_boxes = []
        if getattr(vlm, 'enabled', True):
            for obj in vlm.detect_objects(image):
                obj_overlay_boxes.append({
                    'bbox': obj['bbox'],
                    'label': f"{obj['label']} ({int(obj['conf'] * 100)}%)",
                    'color': (255, 190, 50),  # Cyan-blue for object boxes
                    'status': 'object'
                })

        detections = detect_and_extract_faces(image) if recognition_enabled else []

        if not detections:
            if recognition_enabled:
                with state_lock:
                    recognition_stats['no_face'] += 1
            if obj_overlay_boxes:
                annotated_frame = render_face_overlay(image, obj_overlay_boxes)
                ret, enc = cv2.imencode('.jpg', annotated_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ret:
                    with latest_jpeg_lock:
                        latest_annotated_jpeg = enc.tobytes()
            else:
                with latest_jpeg_lock:
                    latest_annotated_jpeg = jpg_bytes
            return

        full_image_filename = f'{timestamp.strftime("%Y%m%d_%H%M%S_%f")}.jpg'
        full_image_path = os.path.join(FULL_IMAGES_FOLDER, full_image_filename)
        detected_persons = []
        overlay_boxes = list(obj_overlay_boxes)

        for idx, det in enumerate(detections):
            top, right, bottom, left = det['bbox']
            pad = 20
            ctop = max(0, top - pad)
            cleft = max(0, left - pad)
            cbottom = min(image.shape[0], bottom + pad)
            cright = min(image.shape[1], right + pad)
            face_crop = image[ctop:cbottom, cleft:cright]

            if not det['usable'] or det['sface_vec'] is None:
                record_recognition_event(
                    'unusable_quality',
                    person_id=None,
                    similarity=0.0,
                    quality=det['quality'],
                    reason=det['reason']
                )
                if (right - left) >= 28 and (bottom - top) >= 28:
                    overlay_boxes.append({
                        'bbox': (top, right, bottom, left),
                        'label': f"Unclear (Q:{int(det['quality'])})",
                        'color': (0, 180, 255),
                        'status': 'unusable_quality'
                    })
                continue

            sface_vec = det['sface_vec']
            quality = det['quality']
            det_score = det['det_score']

            match_status, matched_pid, sim_score, match_details = match_face_against_enrolled(sface_vec)

            if match_status == 'matched' and matched_pid is not None:
                person_id = matched_pid
            elif match_status == 'inconclusive':
                # Save to unrecognized inspection folder without creating a duplicate identity
                cleanup_old_files(UNRECOGNIZED_FOLDER, 30)
                unrec_name = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_inc_{matched_pid}.jpg"
                cv2.imwrite(os.path.join(UNRECOGNIZED_FOLDER, unrec_name), face_crop)
                matched_label = person_labels.get(matched_pid, matched_pid)
                record_recognition_event(
                    'inconclusive',
                    person_id=matched_pid,
                    similarity=sim_score,
                    quality=quality,
                    reason=f"Ambiguous similarity ({sim_score:.3f}) to {matched_pid}",
                    thumb_file=unrec_name
                )
                overlay_boxes.append({
                    'bbox': (top, right, bottom, left),
                    'label': f"Near {matched_label} ({int(sim_score * 100)}%)",
                    'color': (0, 215, 255),  # Yellow
                    'status': 'inconclusive'
                })
                continue
            else:
                # Unknown face -> pass through multi-frame confirmation buffer
                cand_pid, cand_state, sightings = handle_unknown_candidate(
                    sface_vec, quality, det_score, face_crop, image, timestamp
                )
                if cand_pid is None:
                    cleanup_old_files(UNRECOGNIZED_FOLDER, 30)
                    unrec_name = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_unk_{sightings}.jpg"
                    cv2.imwrite(os.path.join(UNRECOGNIZED_FOLDER, unrec_name), face_crop)
                    record_recognition_event(
                        'unknown_pending',
                        person_id=None,
                        similarity=sim_score,
                        quality=quality,
                        reason=f"Sighting {sightings}/{CANDIDATE_FRAMES_REQUIRED}" if cand_state == 'unknown_pending' else "Below enrollment quality threshold",
                        thumb_file=unrec_name
                    )
                    overlay_boxes.append({
                        'bbox': (top, right, bottom, left),
                        'label': f"Verifying {sightings}/{CANDIDATE_FRAMES_REQUIRED}" if cand_state == 'unknown_pending' else f"Unknown (Q:{int(quality)})",
                        'color': (0, 215, 255),  # Yellow
                        'status': 'unknown_pending'
                    })
                    continue
                person_id = cand_pid
                print(f"[*] Confirmed new person after {sightings} consistent frames: {person_id}")

            # Matched or newly confirmed enrolled person
            added = False
            thumb_rel = None
            is_new_candidate = (match_status != 'matched')
            should_save_face = (
                is_new_candidate or
                (quality >= MIN_QUALITY_FOR_ENROLL and det_score >= ENROLL_DET_CONF_THRESHOLD and sim_score >= 0.50)
            )
            if should_save_face:
                person_folder = os.path.join(FACES_FOLDER, person_id)
                os.makedirs(person_folder, exist_ok=True)
                active_gallery = [s.get('file') for s in identity_profiles.get(person_id, {}).get('sface', [])]
                cleanup_old_files(person_folder, MAX_FILES_PER_PERSON, protected_files=active_gallery)

                face_filename = f'{timestamp.strftime("%Y%m%d_%H%M%S_%f")}_face{idx}.jpg'
                face_path = os.path.join(person_folder, face_filename)
                cv2.imwrite(face_path, face_crop)
                thumb_rel = f"{person_id}/{face_filename}"

                added = add_sface_sample_smart(person_id, sface_vec, quality, det_score, face_filename)

            add_detection(person_id, timestamp)
            detected_persons.append(person_id)

            disp_name = person_labels.get(person_id, person_id)
            overlay_boxes.append({
                'bbox': (top, right, bottom, left),
                'label': f"{disp_name} ({int(sim_score * 100)}%)",
                'color': (46, 204, 113),  # Green
                'status': 'recognized'
            })

            record_recognition_event(
                'recognized',
                person_id=person_id,
                similarity=sim_score if sim_score > 0 else 1.0,
                quality=quality,
                reason="Gallery updated" if added else "Matched enrolled identity",
                thumb_file=thumb_rel
            )

        # Render face + object overlays onto frame for real-time live streaming
        if overlay_boxes:
            annotated_frame = render_face_overlay(image, overlay_boxes)
            ret, enc = cv2.imencode('.jpg', annotated_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret:
                with latest_jpeg_lock:
                    latest_annotated_jpeg = enc.tobytes()
        else:
            with latest_jpeg_lock:
                latest_annotated_jpeg = jpg_bytes

        if detected_persons:
            cleanup_old_files(FULL_IMAGES_FOLDER, MAX_FULL_IMAGES)
            cv2.imwrite(full_image_path, image)
            save_profiles()
            print(f"[{timestamp.strftime('%H:%M:%S')}] Recognized: {', '.join(detected_persons)}")

    except Exception as e:
        print(f"Error processing frame: {e}")
        import traceback
        traceback.print_exc()

RECORDINGS_FOLDER = os.path.join(DATA_FOLDER, 'recordings')
os.makedirs(RECORDINGS_FOLDER, exist_ok=True)
stream_wake_event = threading.Event()
door_recording_lock = threading.Lock()
door_recording_active = False
door_recording_until = 0.0
door_video_writer = None
door_recording_path = None
door_recording_frames = 0
door_recording_start_ts = 0.0
door_video_w = 0
door_video_h = 0
door_recording_timer = None
door_open_seq = 0
RECORDING_TARGET_FPS = 10.0

def _finalize_all_recordings():
    with door_recording_lock:
        if door_video_writer is not None:
            _close_recording_writer_locked()

import atexit
atexit.register(_finalize_all_recordings)

def _schedule_recording_finalize(delay_s):
    global door_recording_timer
    if door_recording_timer is not None:
        try:
            door_recording_timer.cancel()
        except Exception:
            pass
    door_recording_timer = threading.Timer(max(0.2, float(delay_s)), _finalize_expired_recording)
    door_recording_timer.daemon = True
    door_recording_timer.start()

def _finalize_expired_recording():
    with door_recording_lock:
        if door_recording_active and time.time() >= door_recording_until:
            _close_recording_writer_locked()
        elif not door_recording_active and door_video_writer is not None:
            _close_recording_writer_locked()

def _prune_old_recordings_if_low_disk(min_free_gb=2.0):
    """Prune oldest recordings FIFO when free disk space drops below min_free_gb."""
    try:
        if shutil.disk_usage('.').free / (1024 ** 3) >= min_free_gb:
            return 0
        files = []
        for r, _ds, fs in os.walk(RECORDINGS_FOLDER):
            for f in fs:
                if f.endswith(('.mp4', '.avi')):
                    fp = os.path.join(r, f)
                    try:
                        files.append((os.path.getmtime(fp), fp))
                    except OSError:
                        pass
        files.sort()
        pruned = 0
        for _mt, fp in files:
            if shutil.disk_usage('.').free / (1024 ** 3) >= min_free_gb:
                break
            try:
                os.remove(fp)
                pruned += 1
            except OSError:
                pass
        if pruned:
            event_db.log_audit('system', 'retention_prune', RECORDINGS_FOLDER, f'pruned={pruned}')
        return pruned
    except Exception:
        return 0

def _disk_retention_janitor_thread():
    while stream_active:
        _prune_old_recordings_if_low_disk(min_free_gb=2.0)
        for _ in range(90):
            if not stream_active:
                break
            time.sleep(10.0)

threading.Thread(target=_disk_retention_janitor_thread, daemon=True).start()

def start_door_recording(duration_s=20.0, bump_seq=True):
    """Arm live MJPEG recording immediately on DOOR_OPEN (<150ms target)."""
    global door_recording_active, door_recording_until, door_open_seq
    with door_recording_lock:
        door_recording_active = True
        door_recording_until = time.time() + float(duration_s)
        if bump_seq:
            door_open_seq += 1
        _schedule_recording_finalize(float(duration_s) + 0.3)
    stream_wake_event.set()
    print(f"[REC] Door recording armed for up to {duration_s:.0f}s")

def stop_door_recording_tail(tail_s=5.0):
    """Keep recording briefly after DOOR_CLOSED, then finalize video."""
    global door_recording_until
    with door_recording_lock:
        if door_recording_active:
            door_recording_until = min(door_recording_until, time.time() + float(tail_s))
            _schedule_recording_finalize(max(0.2, door_recording_until - time.time()) + 0.3)

def _close_recording_writer_locked():
    global door_video_writer, door_recording_active, door_recording_path, door_recording_frames, door_recording_start_ts, door_video_w, door_video_h
    if door_video_writer is not None:
        try:
            door_video_writer.release()
        except Exception:
            pass
        if door_recording_frames == 0 and door_recording_path and os.path.exists(door_recording_path):
            try:
                os.remove(door_recording_path)
            except Exception:
                pass
        else:
            print(f"[REC] Saved recording ({door_recording_frames} frames): {door_recording_path}")
        door_video_writer = None
    door_recording_active = False
    door_recording_path = None
    door_recording_frames = 0
    door_recording_start_ts = 0.0
    door_video_w = 0
    door_video_h = 0

def _write_recording_frame(img):
    global door_video_writer, door_recording_path, door_recording_frames, door_recording_start_ts, door_video_w, door_video_h
    now = time.time()
    with door_recording_lock:
        if not door_recording_active:
            if door_video_writer is not None:
                _close_recording_writer_locked()
            return
        if now > door_recording_until:
            _close_recording_writer_locked()
            return
        if door_video_writer is None:
            now_dt = datetime.now()
            day_dir = os.path.join(RECORDINGS_FOLDER, now_dt.strftime('%Y'), now_dt.strftime('%m'), now_dt.strftime('%d'))
            os.makedirs(day_dir, exist_ok=True)
            base_name = now_dt.strftime('%H-%M-%S')
            candidate = os.path.join(day_dir, f"{base_name}.mp4")
            if os.path.exists(candidate):
                candidate = os.path.join(day_dir, f"{base_name}_{now_dt.strftime('%f')[:3]}.mp4")
            door_recording_path = candidate
            h, w = img.shape[:2]
            door_video_w, door_video_h = w, h
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            door_video_writer = cv2.VideoWriter(door_recording_path, fourcc, RECORDING_TARGET_FPS, (w, h))
            door_recording_frames = 0
            door_recording_start_ts = now
            print(f"[REC] Started live recording -> {door_recording_path} ({w}x{h} @ {RECORDING_TARGET_FPS:.0f}fps)")
        if door_video_writer is not None and door_video_writer.isOpened():
            frame_to_write = img
            if img.shape[1] != door_video_w or img.shape[0] != door_video_h:
                frame_to_write = cv2.resize(img, (door_video_w, door_video_h))
            elapsed = max(0.0, now - door_recording_start_ts)
            target_count = max(1, int(round(elapsed * RECORDING_TARGET_FPS)))
            repeat = max(1, min(15, target_count - door_recording_frames))
            for _ in range(repeat):
                door_video_writer.write(frame_to_write)
                door_recording_frames += 1

def mjpeg_consumer_thread():
    """Background thread to consume MJPEG stream from ESP32-CAM and buffer validated frames"""
    global stream_active, latest_jpeg, camera_online, active_camera_ip, stream_fps, last_frame_received_time
    print("Starting MJPEG consumer thread")
    socket.setdefaulttimeout(10.0)
    last_standby_status_poll = 0.0

    while stream_active:
        candidate_urls = get_candidate_stream_urls()
        resp = None
        connected_url = None

        for test_url in candidate_urls:
            try:
                req = urllib.request.Request(test_url, headers={'Connection': 'close', 'User-Agent': 'ESP32-Dashboard'})
                r = urllib.request.urlopen(req, timeout=3.0)
                if r.status == 200:
                    try:
                        r.fp.raw._sock.settimeout(10.0)
                    except Exception:
                        pass
                    resp = r
                    connected_url = test_url
                    break
                r.close()
            except Exception:
                pass

        if not resp:
            if camera_online:
                board_alive = False
                try:
                    ping_ip = active_camera_ip or (CANDIDATE_IPS[0] if CANDIDATE_IPS else '192.168.29.10')
                    with urllib.request.urlopen(f"http://{ping_ip}:80/ping", timeout=1.0) as pr:
                        board_alive = (pr.status == 200)
                except Exception:
                    board_alive = False
                if not board_alive:
                    notification_manager.emit(NotificationEvent(
                        event_type='CAMERA_OFFLINE',
                        title="Camera Offline",
                        message="Unable to reach ESP32-CAM.",
                        severity='critical',
                        dedup_key="CAMERA_OFFLINE",
                    ))
            camera_online = False
            stream_fps = 0.0
            _finalize_expired_recording()
            now_poll = time.time()
            if now_poll - last_standby_status_poll >= 4.0:
                last_standby_status_poll = now_poll
                threading.Thread(target=refresh_camera_hardware_status, daemon=True).start()
            stream_wake_event.wait(timeout=0.15)
            stream_wake_event.clear()
            continue

        try:
            current_ip = connected_url.split("://")[1].split(":")[0]
            active_camera_ip = current_ip
            camera_online = True
            last_frame_received_time = time.time()
            print(f"Connected to MJPEG stream ({connected_url}) - Streaming active")
            threading.Thread(target=refresh_camera_hardware_status, daemon=True).start()
            threading.Thread(target=_apply_camera_defaults, args=(current_ip,), daemon=True).start()

            bytes_data = bytes()
            frame_count = 0
            start_time = time.time()

            while stream_active:
                if time.time() - last_frame_received_time > 10.0:
                    print("Frame stream stalled for >10.0s, reconnecting to find active IP...")
                    break

                chunk = resp.read(8192)
                if not chunk:
                    break

                bytes_data += chunk

                if len(bytes_data) > 250000:
                    last_soi = bytes_data.rfind(b'\xff\xd8')
                    bytes_data = bytes_data[last_soi:] if last_soi != -1 else bytes()

                latest_valid_jpg = None
                while True:
                    b = bytes_data.find(b'\xff\xd9')
                    if b == -1:
                        break
                    a = bytes_data.rfind(b'\xff\xd8', 0, b)
                    if a == -1:
                        bytes_data = bytes_data[b+2:]
                        continue

                    candidate_jpg = bytes_data[a:b+2]
                    bytes_data = bytes_data[b+2:]

                    if len(candidate_jpg) >= 400:
                        arr = np.frombuffer(candidate_jpg, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None and img.shape[0] >= 60 and img.shape[1] >= 60:
                            latest_valid_jpg = candidate_jpg
                            frame_count += 1
                            if door_recording_active:
                                _write_recording_frame(img)

                if latest_valid_jpg is not None:
                    now = time.time()
                    last_frame_received_time = now
                    camera_online = True

                    with latest_jpeg_lock:
                        latest_jpeg = latest_valid_jpg

                    if recognition_enabled or getattr(vlm, 'enabled', False):
                        try:
                            frame_queue.put_nowait(latest_valid_jpg)
                        except queue.Full:
                            try:
                                frame_queue.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                frame_queue.put_nowait(latest_valid_jpg)
                            except queue.Full:
                                pass

                    elapsed = now - start_time
                    if elapsed >= 5.0:
                        stream_fps = frame_count / elapsed
                        frame_count = 0
                        start_time = now

            resp.close()

        except Exception as e:
            if camera_online:
                notification_manager.emit(NotificationEvent(
                    event_type='CAMERA_OFFLINE',
                    title="Camera Offline",
                    message=f"ESP32-CAM stream disconnected: {e}",
                    severity='critical',
                    dedup_key="CAMERA_OFFLINE",
                ))
            camera_online = False
            stream_fps = 0.0
            if resp:
                try:
                    resp.close()
                except Exception:
                    pass
            print(f"Camera disconnected: {e} - Re-scanning...")
            time.sleep(1.0)

    camera_online = False
    print("MJPEG consumer thread stopped")

def ai_worker_thread():
    """Background thread to process AI face recognition on the latest frame"""
    global stream_active
    print("AI Face Recognition worker active (YuNet + SFace 128-D Hypersphere)")
    while stream_active:
        if not recognition_enabled and not getattr(vlm, 'enabled', False):
            time.sleep(0.15)
            continue
        try:
            jpg = frame_queue.get(timeout=1.0)
            if not recognition_enabled and not getattr(vlm, 'enabled', False):
                continue
            process_frame(jpg)
            time.sleep(0.18)  # Keep dual-model CPU load <= 40%
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Error in AI worker thread: {e}")

def backup_thread():
    while True:
        time.sleep(BACKUP_INTERVAL)
        save_detections()
        save_profiles()

def add_detection(person_id, timestamp):
    with state_lock:
        if person_id not in person_detections:
            person_detections[person_id] = []
        person_detections[person_id].append(timestamp)
        cutoff = datetime.now() - timedelta(days=7)
        person_detections[person_id] = [ts for ts in person_detections[person_id] if ts > cutoff]

def get_bucketed_detections(person_id, hours=24):
    with state_lock:
        if person_id not in person_detections:
            return []
        cutoff = datetime.now() - timedelta(hours=hours)
        recent = [ts for ts in person_detections[person_id] if ts > cutoff]
    if not recent:
        return []
    buckets = defaultdict(int)
    for ts in recent:
        bucket_time = ts.replace(second=(ts.second // BUCKET_SIZE) * BUCKET_SIZE, microsecond=0)
        buckets[bucket_time] += 1
    return [{'time': k.isoformat(), 'count': v} for k, v in sorted(buckets.items())]

def cleanup_old_files(folder, max_files, protected_files=None):
    files = glob.glob(os.path.join(folder, '*.jpg'))
    if len(files) >= max_files:
        files.sort(key=os.path.getmtime)
        protected = set(protected_files or [])
        deletable = [f for f in files if os.path.basename(f) not in protected]
        for f in deletable[:max(0, len(files) - max_files + 1)]:
            try:
                os.remove(f)
            except Exception:
                pass

# ============================================================================
# PHASE 3: DUPLICATE IDENTITY ANALYSIS & TRANSACTIONAL MERGE
# ============================================================================
def compute_duplicate_candidates():
    """
    Identify candidate duplicate profiles using actual facial embeddings (SFace Cosine Similarity
    + legacy dlib Euclidean distance) and sample quality metrics.
    Never merges automatically; returns structured evidence for human review.
    """
    with state_lock:
        person_folders = sorted(glob.glob(os.path.join(FACES_FOLDER, 'person_*')))
        pids = [os.path.basename(f) for f in person_folders]

        candidates = []
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                p1, p2 = pids[i], pids[j]
                prof1 = identity_profiles.get(p1, {})
                prof2 = identity_profiles.get(p2, {})
                sf1 = prof1.get('sface', [])
                sf2 = prof2.get('sface', [])

                max_cos = 0.0
                mean_cos = 0.0
                centroid_cos = 0.0
                if sf1 and sf2:
                    pair_sims = [cosine_sim(a['vec'], b['vec']) for a in sf1 for b in sf2]
                    max_cos = float(max(pair_sims))
                    mean_cos = float(np.mean(pair_sims))
                    if prof1.get('centroid') is not None and prof2.get('centroid') is not None:
                        centroid_cos = cosine_sim(prof1['centroid'], prof2['centroid'])

                # Also check legacy dlib encodings if present
                dlib_min_dist = None
                e1 = known_face_encodings.get(p1, [])
                e2 = known_face_encodings.get(p2, [])
                if e1 and e2:
                    dists = [float(np.linalg.norm(np.asarray(a) - np.asarray(b))) for a in e1 for b in e2]
                    dlib_min_dist = min(dists)

                # Determine if facial evidence justifies recommending human review
                is_strong_sface = (max_cos >= 0.45 or centroid_cos >= 0.40)
                is_moderate_sface = (max_cos >= 0.363 or centroid_cos >= 0.33)
                is_strong_dlib = (dlib_min_dist is not None and dlib_min_dist <= 0.44)

                if not (is_strong_sface or is_moderate_sface or is_strong_dlib):
                    continue

                if max_cos >= 0.55 or centroid_cos >= 0.48 or (dlib_min_dist is not None and dlib_min_dist <= 0.36):
                    tier = "High Confidence Duplicate"
                    confidence_pct = min(99.0, round(max(max_cos * 115.0, (1.0 - (dlib_min_dist or 0.5)) * 130.0), 1))
                else:
                    tier = "Likely Duplicate (Review Recommended)"
                    confidence_pct = min(88.0, round(max(max_cos * 110.0, (1.0 - (dlib_min_dist or 0.55)) * 120.0), 1))

                imgs1 = [os.path.basename(x) for x in sorted(glob.glob(os.path.join(FACES_FOLDER, p1, '*.jpg')), key=os.path.getmtime, reverse=True)]
                imgs2 = [os.path.basename(x) for x in sorted(glob.glob(os.path.join(FACES_FOLDER, p2, '*.jpg')), key=os.path.getmtime, reverse=True)]
                q1 = round(float(np.mean([s['quality'] for s in sf1])), 1) if sf1 else 0.0
                q2 = round(float(np.mean([s['quality'] for s in sf2])), 1) if sf2 else 0.0

                # Recommend canonical profile based on sample count, quality, and detection history
                score_p1 = len(sf1) * 3 + q1 * 0.5 + len(person_detections.get(p1, []))
                score_p2 = len(sf2) * 3 + q2 * 0.5 + len(person_detections.get(p2, []))
                recommended_canonical = p1 if score_p1 >= score_p2 else p2

                reasons = []
                if max_cos > 0:
                    reasons.append(f"SFace peak cosine similarity {max_cos:.3f} (threshold {SFACE_MATCH_STANDARD})")
                if centroid_cos > 0:
                    reasons.append(f"Profile centroid similarity {centroid_cos:.3f}")
                if dlib_min_dist is not None:
                    reasons.append(f"ResNet-34 min Euclidean distance {dlib_min_dist:.3f}")

                candidates.append({
                    'pair_id': f"{p1}__{p2}",
                    'person_a': {
                        'id': p1,
                        'label': person_labels.get(p1, ''),
                        'image_count': len(imgs1),
                        'sample_images': imgs1[:4],
                        'sface_count': len(sf1),
                        'avg_quality': q1,
                        'detections': len(person_detections.get(p1, []))
                    },
                    'person_b': {
                        'id': p2,
                        'label': person_labels.get(p2, ''),
                        'image_count': len(imgs2),
                        'sample_images': imgs2[:4],
                        'sface_count': len(sf2),
                        'avg_quality': q2,
                        'detections': len(person_detections.get(p2, []))
                    },
                    'tier': tier,
                    'confidence_pct': confidence_pct,
                    'max_cosine_similarity': round(max_cos, 3),
                    'mean_cosine_similarity': round(mean_cos, 3),
                    'centroid_similarity': round(centroid_cos, 3),
                    'dlib_min_distance': round(dlib_min_dist, 3) if dlib_min_dist is not None else None,
                    'recommended_canonical': recommended_canonical,
                    'evidence': reasons
                })

        candidates.sort(key=lambda x: x['confidence_pct'], reverse=True)
        return candidates

# ============================================================================
# PHASE 4: OWNERSHIP AUDIT & 3-TIER SAFE PERSON DELETION
# ============================================================================
def analyze_person_storage_ownership(person_id):
    """
    Trace every persistence location for person_id and verify whether associated
    full-frame images are exclusively owned by person_id or shared with another person.
    """
    with state_lock:
        person_folder = os.path.join(FACES_FOLDER, person_id)
        face_files = sorted(glob.glob(os.path.join(person_folder, '*.jpg'))) if os.path.exists(person_folder) else []

        # Collect timestamp prefixes for this person's face crops
        my_timestamps = set()
        for f in face_files:
            bname = os.path.basename(f)
            if '_face' in bname:
                my_timestamps.add(bname.split('_face')[0])

        # Collect timestamp prefixes across all OTHER persons to prevent deleting shared full frames
        other_timestamps = set()
        for other_folder in glob.glob(os.path.join(FACES_FOLDER, 'person_*')):
            other_id = os.path.basename(other_folder)
            if other_id == person_id:
                continue
            for f in glob.glob(os.path.join(other_folder, '*.jpg')):
                bname = os.path.basename(f)
                if '_face' in bname:
                    other_timestamps.add(bname.split('_face')[0])

        exclusive_full_images = []
        shared_full_images = []
        for ts_str in sorted(my_timestamps):
            full_path = os.path.join(FULL_IMAGES_FOLDER, f"{ts_str}.jpg")
            if os.path.exists(full_path):
                if ts_str in other_timestamps:
                    shared_full_images.append(os.path.basename(full_path))
                else:
                    exclusive_full_images.append(os.path.basename(full_path))

        sface_count = len(identity_profiles.get(person_id, {}).get('sface', []))
        dlib_count = len(known_face_encodings.get(person_id, []))
        detection_count = len(person_detections.get(person_id, []))
        enrolled = identity_profiles.get(person_id, {}).get('enrolled', True)

        return {
            'person_id': person_id,
            'label': person_labels.get(person_id, ''),
            'enrolled': enrolled,
            'sface_embeddings_count': sface_count,
            'legacy_dlib_encodings_count': dlib_count,
            'face_crop_images_count': len(face_files),
            'face_crop_filenames': [os.path.basename(f) for f in face_files[:10]],
            'exclusive_full_images_count': len(exclusive_full_images),
            'exclusive_full_filenames': exclusive_full_images,
            'shared_full_images_preserved_count': len(shared_full_images),
            'shared_full_filenames': shared_full_images,
            'detection_records_count': detection_count
        }

# ============================================================================
# FLASK ROUTES & API ENDPOINTS
# ============================================================================
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/live')
def live_feed():
    """Proxy live MJPEG stream from ESP32-CAM to web browsers with graceful offline fallback"""
    def generate():
        last_sent = None
        last_sent_time = 0.0
        while True:
            now = time.time()
            with latest_jpeg_lock:
                if recognition_enabled and show_face_overlay and latest_annotated_jpeg is not None:
                    frame = latest_annotated_jpeg
                else:
                    frame = latest_jpeg

            # Grace period of 12.0s prevents transient Wi-Fi retransmits from breaking live stream
            is_alive = camera_online and (now - last_frame_received_time < 12.0) and (frame is not None)

            if is_alive:
                if frame is not last_sent or (now - last_sent_time >= 0.25):
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                    last_sent = frame
                    last_sent_time = now
                time.sleep(0.02)
            else:
                placeholder = SENSOR_ERROR_FRAME if camera_online else OFFLINE_FRAME
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + placeholder + b'\r\n')
                last_sent = None
                time.sleep(0.5)

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/toggle_overlay', methods=['POST'])
def toggle_overlay():
    """Toggle real-time face overlay bounding boxes on the live stream"""
    global show_face_overlay
    with state_lock:
        show_face_overlay = not show_face_overlay
        current = show_face_overlay
    return jsonify({'success': True, 'show_face_overlay': current})

@app.route('/api/recognition/toggle', methods=['POST'])
def toggle_recognition():
    """Toggle the entire AI face recognition pipeline on or off"""
    global recognition_enabled, latest_annotated_jpeg
    req_data = request.get_json(silent=True) or {}
    with state_lock:
        if 'enabled' in req_data:
            recognition_enabled = bool(req_data['enabled'])
        else:
            recognition_enabled = not recognition_enabled

        if not recognition_enabled:
            with latest_jpeg_lock:
                latest_annotated_jpeg = None
            while not frame_queue.empty():
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    break
            pending_candidate_tracks.clear()
            recognition_stats['last_status'] = 'Recognition is turned off'
        else:
            recognition_stats['last_status'] = 'Recognition active — waiting for faces'

        current = recognition_enabled

    return jsonify({'success': True, 'recognition_enabled': current})

@app.route('/stream/status')
def stream_status():
    """Get stream connection status, camera health, and live recognition telemetry"""
    now = time.time()
    is_alive = camera_online and (now - last_frame_received_time < 12.0)
    with state_lock:
        stats_copy = dict(recognition_stats)
        events_copy = list(recent_recognition_events)[:8]
        pending_count = len(pending_candidate_tracks)
        overlay_on = show_face_overlay
        recog_on = recognition_enabled
        cam_info = cached_camera_settings.get('camera', {})
        sensor_ready = bool(cam_info.get('ready', False)) if isinstance(cam_info, dict) else False
        sensor_active = bool(cam_info.get('sensor_active', is_alive)) if isinstance(cam_info, dict) else is_alive
        board_online = bool(cached_camera_settings.get('online', False)) or is_alive
        board_ip = active_camera_ip or (CANDIDATE_IPS[0] if (board_online and CANDIDATE_IPS) else None)

    return jsonify({
        'active': stream_active,
        'camera_online': is_alive,
        'board_online': board_online,
        'sensor_ready': sensor_ready,
        'cam_sensor_active': sensor_active,
        'recording_active': door_recording_active,
        'door_open_seq': door_open_seq,
        'ip': board_ip,
        'fps': round(stream_fps, 1) if is_alive else 0.0,
        'temp_c': cached_camera_settings.get('temp_c'),
        'last_frame_seconds_ago': round(now - last_frame_received_time, 1) if last_frame_received_time > 0 else None,
        'url': f"http://{active_camera_ip}:81/stream" if active_camera_ip else "http://192.168.29.10:81/stream",
        'total_persons': len(glob.glob(os.path.join(FACES_FOLDER, 'person_*'))),
        'total_images': len(glob.glob(os.path.join(FULL_IMAGES_FOLDER, '*.jpg'))),
        'pending_candidates': pending_count if recog_on else 0,
        'show_face_overlay': overlay_on,
        'recognition_enabled': recog_on,
        'recognition_stats': stats_copy,
        'recent_events': events_copy,
        'vlm': vlm.get_status()
    })

@app.route('/stream/reconnect', methods=['POST'])
def stream_reconnect():
    global stream_active, mjpeg_thread
    stream_active = False
    time.sleep(1.5)
    stream_active = True
    mjpeg_thread = threading.Thread(target=mjpeg_consumer_thread, daemon=True)
    mjpeg_thread.start()
    return jsonify({'success': True, 'message': 'Stream reconnecting'}), 200

IDENTITIES_FILE = os.path.join('data', 'person_identities.json')

def _load_identities():
    if os.path.exists(IDENTITIES_FILE):
        try:
            with open(IDENTITIES_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
    return {}

def _save_identities():
    os.makedirs(os.path.dirname(IDENTITIES_FILE), exist_ok=True)
    with open(IDENTITIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(person_identities, f, indent=2)

person_identities = _load_identities()

@app.route('/')
def index():
    """Main dark-theme dashboard route"""
    with state_lock:
        person_folders = sorted(glob.glob(os.path.join(FACES_FOLDER, 'person_*')))
        persons_data = []

        for folder in person_folders:
            person_id = os.path.basename(folder)
            image_files = sorted(glob.glob(os.path.join(folder, '*.jpg')), key=os.path.getmtime, reverse=True)
            latest_images = image_files[:5]

            total_detections = len(person_detections.get(person_id, []))
            last_seen = None
            if person_id in person_detections and person_detections[person_id]:
                last_seen = max(person_detections[person_id])

            prof = identity_profiles.get(person_id, {})
            sface_samples = prof.get('sface', [])
            enrolled = prof.get('enrolled', True)
            avg_quality = round(float(np.mean([s['quality'] for s in sface_samples])), 1) if sface_samples else 0.0
            quality_by_file = {s.get('file'): round(float(s.get('quality', 0.0)), 1) for s in sface_samples}

            images_data = []
            for img_path in latest_images:
                img_name = os.path.basename(img_path)
                try:
                    timestamp_str = img_name.split('_face')[0]
                    img_time = datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S_%f')
                    time_display = img_time.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    time_display = "Unknown"

                images_data.append({
                    'filename': img_name,
                    'time': time_display,
                    'quality': quality_by_file.get(img_name)
                })

            persons_data.append({
                'id': person_id,
                'label': person_labels.get(person_id, ''),
                'identity': person_identities.get(person_id, {}),
                'enrolled': enrolled,
                'total_images': len(image_files),
                'total_detections': total_detections,
                'encodings': len(sface_samples) or len(known_face_encodings.get(person_id, [])),
                'sface_count': len(sface_samples),
                'avg_quality': avg_quality,
                'last_seen': last_seen,
                'images': images_data
            })

    total_full_images = len(glob.glob(os.path.join(FULL_IMAGES_FOLDER, '*.jpg')))
    duplicate_candidates = compute_duplicate_candidates()

    return render_template(
        'index.html',
        persons=persons_data,
        total_persons=len(persons_data),
        total_images=total_full_images,
        duplicate_count=len(duplicate_candidates),
        model=MODEL_NAME,
        tolerance=f"Cosine >= {SFACE_MATCH_STANDARD}",
        now=datetime.now()
    )

@app.route('/api/persons')
def get_all_persons():
    """Get list of all persons with embedding & quality metadata"""
    with state_lock:
        person_folders = sorted(glob.glob(os.path.join(FACES_FOLDER, 'person_*')))
        persons = []
        for folder in person_folders:
            person_id = os.path.basename(folder)
            images = sorted(glob.glob(os.path.join(folder, '*.jpg')), key=os.path.getmtime, reverse=True)
            prof = identity_profiles.get(person_id, {})
            sface_samples = prof.get('sface', [])
            avg_q = round(float(np.mean([s['quality'] for s in sface_samples])), 1) if sface_samples else 0.0

            persons.append({
                'id': person_id,
                'label': person_labels.get(person_id, ''),
                'enrolled': prof.get('enrolled', True),
                'image_count': len(images),
                'detection_count': len(person_detections.get(person_id, [])),
                'sface_count': len(sface_samples),
                'avg_quality': avg_q,
                'sample_image': os.path.basename(images[0]) if images else None,
                'all_images': [os.path.basename(x) for x in images[:24]]
            })
        return jsonify(persons)

@app.route('/api/person/<person_id>/label', methods=['POST'])
def update_person_label(person_id):
    """Assign or update a human-readable display name for a person_id"""
    data = request.get_json(silent=True) or request.form or {}
    raw_label = data.get('label') if 'label' in data else request.values.get('label', '')
    label = (raw_label or '').strip()
    with state_lock:
        if label:
            person_labels[person_id] = label
        else:
            person_labels.pop(person_id, None)
        save_labels()
    return jsonify({'success': True, 'person_id': person_id, 'label': label})

@app.route('/api/person/<person_id>/identity', methods=['POST'])
def update_person_identity(person_id):
    """Assign relationship/identity category (Family/Friend/Neighbour/Custom) to a person"""
    data = request.get_json(silent=True) or {}
    category = str(data.get('category') or '').strip()
    role = str(data.get('role') or '').strip()
    if not category:
        with state_lock:
            person_identities.pop(person_id, None)
            _save_identities()
        return jsonify({'ok': True, 'person_id': person_id, 'identity': {}, 'display': ''})

    entry = {'category': category, 'role': role}
    display = f"{category}: {role}" if role else category
    with state_lock:
        person_identities[person_id] = entry
        _save_identities()
    try:
        event_db.log_audit('user', 'set_identity', person_id, display)
    except Exception:
        pass
    return jsonify({'ok': True, 'person_id': person_id, 'identity': entry, 'display': display})

@app.route('/api/person/<person_id>/samples')
def get_person_samples(person_id):
    """Inspect face samples and embedding quality metrics for a person with optional pagination"""
    with state_lock:
        folder = os.path.join(FACES_FOLDER, person_id)
        if not os.path.exists(folder):
            return jsonify({'error': f'{person_id} not found'}), 404
        images = sorted(glob.glob(os.path.join(folder, '*.jpg')), key=os.path.getmtime, reverse=True)
        prof = identity_profiles.get(person_id, {})
        sface_map = {s.get('file'): s for s in prof.get('sface', [])}

        total_count = len(images)
        try:
            offset = int(request.args.get('offset', 0))
            limit = int(request.args.get('limit', 50))
        except (ValueError, TypeError):
            offset = 0
            limit = 50

        if limit <= 0:
            sliced_images = images[offset:]
        else:
            sliced_images = images[offset:offset + limit]

        items = []
        for img_path in sliced_images:
            fname = os.path.basename(img_path)
            meta = sface_map.get(fname, {})
            items.append({
                'filename': fname,
                'in_gallery': fname in sface_map,
                'quality': round(float(meta.get('quality', 0.0)), 1) if meta else None,
                'det_score': round(float(meta.get('det_score', 0.0)), 3) if meta else None
            })
        return jsonify({
            'person_id': person_id,
            'label': person_labels.get(person_id, ''),
            'enrolled': prof.get('enrolled', True),
            'gallery_size': len(prof.get('sface', [])),
            'total_count': total_count,
            'offset': offset,
            'limit': limit,
            'has_more': (offset + len(sliced_images)) < total_count,
            'samples': items
        })

@app.route('/api/person/<person_id>/sample/<filename>', methods=['DELETE'])
def delete_single_sample(person_id, filename):
    """Remove a single bad face sample image and its embedding from a person's profile"""
    safe_name = os.path.basename(filename)
    with state_lock:
        img_path = os.path.join(FACES_FOLDER, person_id, safe_name)
        if os.path.exists(img_path):
            os.remove(img_path)
        prof = identity_profiles.get(person_id)
        if prof and prof.get('sface'):
            prof['sface'] = [s for s in prof['sface'] if s.get('file') != safe_name]
            recompute_profile_centroid(person_id)
            save_profiles()
    return jsonify({'success': True, 'deleted_sample': safe_name})

@app.route('/api/person/<person_id>/sample/<filename>/toggle-gallery', methods=['POST'])
def toggle_sample_gallery_status(person_id, filename):
    """
    Manually toggle an existing face crop between 'In Gallery' (active for recognition)
    and 'Archived' (retained on disk, but inactive for recognition).
    """
    safe_name = os.path.basename(filename)
    img_path = os.path.join(FACES_FOLDER, person_id, safe_name)
    if not os.path.exists(img_path):
        return jsonify({'success': False, 'error': f'Image {safe_name} not found'}), 404

    with state_lock:
        if person_id not in identity_profiles:
            identity_profiles[person_id] = {
                'sface': [],
                'centroid': None,
                'enrolled': True,
                'created_at': datetime.now().isoformat()
            }
        prof = identity_profiles[person_id]
        samples = prof.get('sface', [])
        existing_idx = next((i for i, s in enumerate(samples) if s.get('file') == safe_name), None)

        if existing_idx is not None:
            # Currently in gallery -> Demote to Archived
            del samples[existing_idx]
            recompute_profile_centroid(person_id)
            save_profiles()
            return jsonify({
                'success': True,
                'filename': safe_name,
                'in_gallery': False,
                'message': f'Moved {safe_name} to Archived (saved on disk, inactive for recognition).'
            })
        else:
            # Currently Archived -> Promote to In Gallery
            feat, q, dconf = extract_sface_for_saved_image(img_path)
            if feat is None:
                crop_im = cv2.imread(img_path)
                if crop_im is not None:
                    norm_im = apply_illumination_normalization(crop_im)
                    padded = cv2.copyMakeBorder(norm_im, 40, 40, 40, 40, cv2.BORDER_REPLICATE)
                    with detector_lock:
                        yunet_detector.setInputSize((padded.shape[1], padded.shape[0]))
                        yunet_detector.setScoreThreshold(0.35)
                        _, faces = yunet_detector.detect(padded)
                        yunet_detector.setScoreThreshold(DETECTION_CONF_THRESHOLD)
                    if faces is not None and len(faces) > 0:
                        best_face = faces[np.argmax(faces[:, -1])]
                        feat = extract_mobilefacenet_feature(padded, best_face)
                        dconf = float(best_face[-1])
                        q = 75.0

            if feat is None:
                return jsonify({
                    'success': False,
                    'error': 'Cannot promote to gallery: No clear facial landmarks could be detected in this crop.'
                }), 400

            new_entry = {
                'vec': feat,
                'quality': float(q),
                'det_score': float(dconf),
                'ts': datetime.now().isoformat(),
                'file': safe_name
            }

            if len(samples) < MAX_ENCODINGS_PER_PERSON:
                samples.append(new_entry)
            else:
                min_q_idx = int(np.argmin([s.get('quality', 0.0) for s in samples]))
                samples[min_q_idx] = new_entry

            recompute_profile_centroid(person_id)
            save_profiles()
            return jsonify({
                'success': True,
                'filename': safe_name,
                'in_gallery': True,
                'quality': round(q, 1),
                'det_score': round(dconf, 2),
                'message': f'Moved {safe_name} to Gallery (now actively used for recognition)!'
            })

@app.route('/api/person/<person_id>/samples/batch', methods=['POST'])
def batch_samples_action(person_id):
    """
    Execute batch action ('gallery', 'archive', 'delete') on multiple selected photos for a person.
    """
    req_data = request.get_json(silent=True) or {}
    action = req_data.get('action')
    filenames = req_data.get('filenames') or []

    if action not in ('gallery', 'archive', 'delete'):
        return jsonify({'success': False, 'error': f"Invalid action: {action}"}), 400
    if not filenames:
        return jsonify({'success': False, 'error': "No images selected"}), 400

    folder = os.path.join(FACES_FOLDER, person_id)
    if not os.path.exists(folder):
        return jsonify({'success': False, 'error': f"Person {person_id} not found"}), 404

    with state_lock:
        if person_id not in identity_profiles:
            identity_profiles[person_id] = {
                'sface': [],
                'centroid': None,
                'enrolled': True,
                'created_at': datetime.now().isoformat()
            }
        prof = identity_profiles[person_id]
        samples = prof.get('sface', [])

        affected_count = 0
        failed_count = 0

        if action == 'archive':
            filenames_set = set(os.path.basename(f) for f in filenames)
            original_len = len(samples)
            prof['sface'] = [s for s in samples if s.get('file') not in filenames_set]
            affected_count = original_len - len(prof['sface'])
            recompute_profile_centroid(person_id)
            save_profiles()
            msg = f"Archived {affected_count} sample(s)."

        elif action == 'delete':
            filenames_set = set(os.path.basename(f) for f in filenames)
            for fname in filenames_set:
                safe_name = os.path.basename(fname)
                p = os.path.join(folder, safe_name)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        affected_count += 1
                    except Exception:
                        pass
            prof['sface'] = [s for s in samples if s.get('file') not in filenames_set]
            recompute_profile_centroid(person_id)
            save_profiles()
            msg = f"Deleted {affected_count} sample(s)."

        elif action == 'gallery':
            existing_files = set(s.get('file') for s in samples)
            for fname in filenames:
                safe_name = os.path.basename(fname)
                if safe_name in existing_files:
                    continue
                img_path = os.path.join(folder, safe_name)
                if not os.path.exists(img_path):
                    failed_count += 1
                    continue
                feat, q, dconf = extract_sface_for_saved_image(img_path)
                if feat is None:
                    crop_im = cv2.imread(img_path)
                    if crop_im is not None:
                        norm_im = apply_illumination_normalization(crop_im)
                        padded = cv2.copyMakeBorder(norm_im, 40, 40, 40, 40, cv2.BORDER_REPLICATE)
                        with detector_lock:
                            yunet_detector.setInputSize((padded.shape[1], padded.shape[0]))
                            yunet_detector.setScoreThreshold(0.35)
                            _, faces = yunet_detector.detect(padded)
                            yunet_detector.setScoreThreshold(DETECTION_CONF_THRESHOLD)
                        if faces is not None and len(faces) > 0:
                            best_face = faces[np.argmax(faces[:, -1])]
                            feat = extract_mobilefacenet_feature(padded, best_face)
                            dconf = float(best_face[-1])
                            q = 75.0
                if feat is not None:
                    new_entry = {
                        'vec': feat,
                        'quality': float(q),
                        'det_score': float(dconf),
                        'ts': datetime.now().isoformat(),
                        'file': safe_name
                    }
                    if len(samples) < MAX_ENCODINGS_PER_PERSON:
                        samples.append(new_entry)
                    else:
                        min_q_idx = int(np.argmin([s.get('quality', 0.0) for s in samples]))
                        samples[min_q_idx] = new_entry
                    existing_files.add(safe_name)
                    affected_count += 1
                else:
                    failed_count += 1

            recompute_profile_centroid(person_id)
            save_profiles()
            msg = f"Added {affected_count} sample(s) to Gallery."
            if failed_count > 0:
                msg += f" ({failed_count} photo(s) had unclear face landmarks)"

        return jsonify({
            'success': True,
            'action': action,
            'affected_count': affected_count,
            'gallery_size': len(prof.get('sface', [])),
            'message': msg
        })

def enroll_image_for_person(person_id, file_bytes, filename_hint="upload"):
    """
    Process an uploaded photo (front, 45-deg angles, tilt, smile):
    - Decodes image bytes
    - Scales down large photos (>1600px)
    - Runs YuNet face detector with relaxed score threshold (0.45) for pose/angle flexibility
    - Extracts 5-point aligned 128-D SFace feature vector
    - Saves crop to FACES_FOLDER/<person_id>/
    - Enrolls embedding into identity_profiles[person_id]
    - Updates centroid
    Returns (success: bool, detail: dict)
    """
    try:
        nparr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return False, {'file': filename_hint, 'status': 'error', 'reason': 'Invalid or unreadable image file'}

        h, w = img.shape[:2]
        max_dim = max(h, w)
        if max_dim > 1600:
            scale = 1600.0 / max_dim
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

        norm_img = apply_illumination_normalization(img)
        nh, nw = norm_img.shape[:2]

        with detector_lock:
            yunet_detector.setInputSize((nw, nh))
            yunet_detector.setScoreThreshold(0.45)
            _, faces = yunet_detector.detect(norm_img)
            yunet_detector.setScoreThreshold(DETECTION_CONF_THRESHOLD)

        if faces is None or len(faces) == 0:
            return False, {'file': filename_hint, 'status': 'no_face', 'reason': 'No face detected in photo'}

        faces_sorted = sorted(faces, key=lambda f: float(f[14]) * float(f[2]) * float(f[3]), reverse=True)
        best_face = faces_sorted[0]
        usable, quality, reason, bbox = assess_face_quality(img, best_face)
        det_score = float(best_face[14])

        sface_vec = extract_mobilefacenet_feature(norm_img, best_face)

        if sface_vec is None:
            return False, {'file': filename_hint, 'status': 'failed', 'reason': 'Could not extract facial embedding'}

        top, right, bottom, left = bbox
        pad = int(max(10, 0.15 * max(bottom - top, right - left)))
        ctop = max(0, top - pad)
        cleft = max(0, left - pad)
        cbottom = min(img.shape[0], bottom + pad)
        cright = min(img.shape[1], right + pad)
        face_crop = img[ctop:cbottom, cleft:cright]
        if face_crop.size == 0:
            face_crop = img[top:bottom, left:right]

        person_folder = os.path.join(FACES_FOLDER, person_id)
        os.makedirs(person_folder, exist_ok=True)
        ts = datetime.now()
        safe_hint = re.sub(r'[^a-zA-Z0-9_-]', '_', os.path.splitext(filename_hint)[0])[:20]
        face_filename = f'{ts.strftime("%Y%m%d_%H%M%S_%f")}_up_{safe_hint}.jpg'
        face_path = os.path.join(person_folder, face_filename)
        cv2.imwrite(face_path, face_crop)

        new_entry = {
            'vec': sface_vec,
            'quality': float(quality) if usable else max(45.0, float(quality)),
            'det_score': det_score,
            'ts': ts.isoformat(),
            'file': face_filename
        }

        with state_lock:
            if person_id not in identity_profiles:
                identity_profiles[person_id] = {
                    'sface': [],
                    'centroid': None,
                    'enrolled': True,
                    'created_at': ts.isoformat()
                }
            prof = identity_profiles[person_id]
            samples = prof['sface']
            if len(samples) < MAX_ENCODINGS_PER_PERSON:
                samples.append(new_entry)
            else:
                min_q_idx = int(np.argmin([s.get('quality', 0.0) for s in samples]))
                samples[min_q_idx] = new_entry
            recompute_profile_centroid(person_id)

        return True, {
            'file': filename_hint,
            'status': 'enrolled',
            'saved_filename': face_filename,
            'quality': round(new_entry['quality'], 1),
            'det_score': round(det_score, 2),
            'reason': 'Enrolled' if usable else f'Enrolled ({reason})'
        }
    except Exception as e:
        print(f"Error enrolling image {filename_hint} for {person_id}: {e}")
        return False, {'file': filename_hint, 'status': 'error', 'reason': str(e)}

@app.route('/api/person/enroll', methods=['POST'])
def enroll_new_person():
    """Enroll a brand new person identity with uploaded face photos (front, side, etc.)"""
    files = request.files.getlist('images') or request.files.getlist('files')
    if not files or all(not f.filename for f in files):
        return jsonify({'success': False, 'error': 'Please select at least one face photo to enroll.'}), 400

    raw_label = request.form.get('label') or request.values.get('label') or ''
    label = raw_label.strip()

    new_num = get_next_person_id()
    person_id = f'person_{new_num}'
    person_folder = os.path.join(FACES_FOLDER, person_id)
    os.makedirs(person_folder, exist_ok=True)

    results = []
    enrolled_count = 0
    for f in files:
        if not f.filename:
            continue
        file_bytes = f.read()
        if not file_bytes:
            continue
        success, detail = enroll_image_for_person(person_id, file_bytes, f.filename)
        results.append(detail)
        if success:
            enrolled_count += 1

    if enrolled_count == 0:
        try:
            os.rmdir(person_folder)
        except Exception:
            pass
        with state_lock:
            identity_profiles.pop(person_id, None)
        return jsonify({
            'success': False,
            'error': 'No clear faces could be detected in the uploaded images. Please ensure photos are clear and face features (eyes, nose, mouth) are visible.'
        }), 400

    with state_lock:
        if label:
            person_labels[person_id] = label
            save_labels()
        save_profiles()

    return jsonify({
        'success': True,
        'person_id': person_id,
        'label': label,
        'enrolled_count': enrolled_count,
        'total_files': len(files),
        'details': results,
        'message': f'Successfully enrolled {label or person_id} with {enrolled_count} face sample(s)!'
    })

@app.route('/api/person/<person_id>/upload-samples', methods=['POST'])
def upload_samples_for_person(person_id):
    """Upload additional face photos to expand an existing person's sample gallery"""
    with state_lock:
        if person_id not in identity_profiles and not os.path.exists(os.path.join(FACES_FOLDER, person_id)):
            return jsonify({'success': False, 'error': f'Person {person_id} not found'}), 404

    files = request.files.getlist('images') or request.files.getlist('files')
    if not files or all(not f.filename for f in files):
        return jsonify({'success': False, 'error': 'No image files provided'}), 400

    results = []
    enrolled_count = 0
    for f in files:
        if not f.filename:
            continue
        file_bytes = f.read()
        if not file_bytes:
            continue
        success, detail = enroll_image_for_person(person_id, file_bytes, f.filename)
        results.append(detail)
        if success:
            enrolled_count += 1

    if enrolled_count > 0:
        save_profiles()

    return jsonify({
        'success': enrolled_count > 0,
        'person_id': person_id,
        'enrolled_count': enrolled_count,
        'total_files': len(files),
        'details': results,
        'message': f'Successfully added {enrolled_count} face sample(s).' if enrolled_count > 0 else 'No valid faces detected in uploaded photos.'
    })

@app.route('/api/duplicates')
def api_duplicates():
    """Return candidate duplicate profiles with facial similarity evidence for side-by-side review"""
    candidates = compute_duplicate_candidates()
    return jsonify({
        'count': len(candidates),
        'model': MODEL_NAME,
        'threshold': SFACE_MATCH_STANDARD,
        'candidates': candidates
    })

@app.route('/api/merge/preview', methods=['POST'])
def preview_merge():
    """Generate a dry-run preview before confirming a profile merge"""
    data = request.json or {}
    source_id = data.get('source_id')
    target_id = data.get('target_id')
    if not source_id or not target_id or source_id == target_id:
        return jsonify({'error': 'Valid distinct source_id and target_id required'}), 400

    with state_lock:
        s_folder = os.path.join(FACES_FOLDER, source_id)
        t_folder = os.path.join(FACES_FOLDER, target_id)
        if not os.path.exists(s_folder) or not os.path.exists(t_folder):
            return jsonify({'error': 'One or both profiles do not exist'}), 404

        s_imgs = glob.glob(os.path.join(s_folder, '*.jpg'))
        t_imgs = glob.glob(os.path.join(t_folder, '*.jpg'))
        s_prof = identity_profiles.get(source_id, {})
        t_prof = identity_profiles.get(target_id, {})
        sf1 = s_prof.get('sface', [])
        sf2 = t_prof.get('sface', [])

        max_cos = 0.0
        centroid_cos = 0.0
        if sf1 and sf2:
            max_cos = float(max(cosine_sim(a['vec'], b['vec']) for a in sf1 for b in sf2))
            if s_prof.get('centroid') is not None and t_prof.get('centroid') is not None:
                centroid_cos = cosine_sim(s_prof['centroid'], t_prof['centroid'])

        dlib_min = None
        e1 = known_face_encodings.get(source_id, [])
        e2 = known_face_encodings.get(target_id, [])
        if e1 and e2:
            dlib_min = float(min(np.linalg.norm(np.asarray(a) - np.asarray(b)) for a in e1 for b in e2))

        low_similarity_warning = (max_cos < SFACE_MATCH_STANDARD and (dlib_min is None or dlib_min > 0.45))

        return jsonify({
            'canonical_target_id': target_id,
            'canonical_label': person_labels.get(target_id, ''),
            'retired_source_id': source_id,
            'retired_label': person_labels.get(source_id, ''),
            'images_to_transfer': len(s_imgs),
            'target_existing_images': len(t_imgs),
            'combined_images_after_cap': min(MAX_FILES_PER_PERSON, len(s_imgs) + len(t_imgs)),
            'source_embeddings': len(sf1),
            'target_embeddings': len(sf2),
            'combined_embeddings_after_cap': min(MAX_ENCODINGS_PER_PERSON, len(sf1) + len(sf2)),
            'detections_to_transfer': len(person_detections.get(source_id, [])),
            'combined_detections': len(person_detections.get(source_id, [])) + len(person_detections.get(target_id, [])),
            'max_cosine_similarity': round(max_cos, 3),
            'centroid_similarity': round(centroid_cos, 3),
            'dlib_min_distance': round(dlib_min, 3) if dlib_min is not None else None,
            'low_similarity_warning': low_similarity_warning
        })

@app.route('/api/merge', methods=['POST'])
def merge_persons():
    """
    Transactional profile merge:
    1. Snapshots backup of encodings, profiles, and detections before mutation
    2. Moves face crops to canonical profile
    3. Merges highest-quality SFace embeddings & recomputes canonical centroid
    4. Reassigns detection records and preserves timestamps
    5. Removes retired profile directory & persists atomically
    """
    try:
        data = request.json or {}
        source_id = data.get('source_id')
        target_id = data.get('target_id')

        if not source_id or not target_id:
            return jsonify({'error': 'Both source_id and target_id required'}), 400
        if source_id == target_id:
            return jsonify({'error': 'Cannot merge a profile with itself'}), 400

        with state_lock:
            source_folder = os.path.join(FACES_FOLDER, source_id)
            target_folder = os.path.join(FACES_FOLDER, target_id)

            if not os.path.exists(source_folder):
                return jsonify({'error': f'{source_id} not found'}), 404
            if not os.path.exists(target_folder):
                return jsonify({'error': f'{target_id} not found'}), 404

            # 1. Create recoverable pre-merge backup snapshot
            snap_dir = os.path.join(BACKUP_FOLDER, f"pre_merge_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{source_id}_to_{target_id}")
            os.makedirs(snap_dir, exist_ok=True)
            for cfg_file in [ENCODINGS_FILE, PROFILES_FILE, DETECTIONS_FILE, LABELS_FILE]:
                if os.path.exists(cfg_file):
                    shutil.copy2(cfg_file, os.path.join(snap_dir, os.path.basename(cfg_file)))

            # 2. Move face images consistently and track any renamed files
            source_images = glob.glob(os.path.join(source_folder, '*.jpg'))
            moved_count = 0
            renamed_map = {}

            for img_path in source_images:
                orig_name = os.path.basename(img_path)
                img_name = orig_name
                target_path = os.path.join(target_folder, img_name)

                if os.path.exists(target_path):
                    base, ext = os.path.splitext(orig_name)
                    counter = 1
                    while os.path.exists(target_path):
                        img_name = f"{base}_merged{counter}{ext}"
                        target_path = os.path.join(target_folder, img_name)
                        counter += 1

                shutil.move(img_path, target_path)
                renamed_map[orig_name] = img_name
                moved_count += 1

            # 3. Merge SFace embeddings (sorted by quality, deduplicated)
            s_prof = identity_profiles.get(source_id, {'sface': []})
            if target_id not in identity_profiles:
                identity_profiles[target_id] = {'sface': [], 'centroid': None, 'enrolled': True, 'created_at': datetime.now().isoformat()}
            t_prof = identity_profiles[target_id]
            t_prof['enrolled'] = True

            combined_sface = list(t_prof.get('sface', []))
            for s in s_prof.get('sface', []):
                s_copy = dict(s)
                if s_copy.get('file') in renamed_map:
                    s_copy['file'] = renamed_map[s_copy['file']]
                # Avoid near-duplicate redundant vectors
                if not combined_sface or max(cosine_sim(s_copy['vec'], ex['vec']) for ex in combined_sface) < 0.93:
                    combined_sface.append(s_copy)

            combined_sface.sort(key=lambda x: x.get('quality', 0.0), reverse=True)
            t_prof['sface'] = combined_sface[:MAX_ENCODINGS_PER_PERSON]
            recompute_profile_centroid(target_id)
            identity_profiles.pop(source_id, None)

            # 4. Merge legacy dlib encodings
            if source_id in known_face_encodings:
                if target_id not in known_face_encodings:
                    known_face_encodings[target_id] = []
                for enc in known_face_encodings[source_id]:
                    if len(known_face_encodings[target_id]) < MAX_ENCODINGS_PER_PERSON:
                        known_face_encodings[target_id].append(enc)
                del known_face_encodings[source_id]

            # 5. Transfer detection records
            if source_id in person_detections:
                if target_id not in person_detections:
                    person_detections[target_id] = []
                person_detections[target_id].extend(person_detections[source_id])
                person_detections[target_id].sort()
                del person_detections[source_id]

            # Preserve custom label if target had none
            if source_id in person_labels:
                if target_id not in person_labels:
                    person_labels[target_id] = person_labels[source_id]
                del person_labels[source_id]
                save_labels()

            # 6. Cleanup excess images in canonical folder and remove retired source folder
            cleanup_old_files(target_folder, MAX_FILES_PER_PERSON)
            shutil.rmtree(source_folder, ignore_errors=True)

            save_profiles()
            save_encodings()
            save_detections()

            return jsonify({
                'success': True,
                'message': f'Successfully merged {source_id} into canonical profile {target_id}',
                'backup_snapshot': snap_dir,
                'moved_images': moved_count,
                'total_encodings': len(t_prof['sface']),
                'total_detections': len(person_detections.get(target_id, []))
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/person/<person_id>/delete-preview')
def delete_person_preview(person_id):
    """
    Preview exact files, embeddings, and detection records that will be removed
    for Option A, Option B, or Option C before execution.
    """
    mode = request.args.get('mode', 'C').upper()
    audit = analyze_person_storage_ownership(person_id)

    if mode == 'A':
        summary = {
            'mode': 'A',
            'mode_title': 'Option A — Remove Identity & Facial Embeddings Only',
            'will_delete': {
                'sface_embeddings': audit['sface_embeddings_count'],
                'legacy_dlib_encodings': audit['legacy_dlib_encodings_count'],
                'face_crop_images': 0,
                'exclusive_full_images': 0,
                'detection_records': 0
            },
            'will_preserve': {
                'face_crop_images': audit['face_crop_images_count'],
                'full_images': audit['exclusive_full_images_count'] + audit['shared_full_images_preserved_count'],
                'detection_records': audit['detection_records_count']
            }
        }
    elif mode == 'B':
        summary = {
            'mode': 'B',
            'mode_title': 'Option B — Remove Identity and Exclusively Owned Images',
            'will_delete': {
                'sface_embeddings': audit['sface_embeddings_count'],
                'legacy_dlib_encodings': audit['legacy_dlib_encodings_count'],
                'face_crop_images': audit['face_crop_images_count'],
                'exclusive_full_images': audit['exclusive_full_images_count'],
                'detection_records': 0
            },
            'will_preserve': {
                'shared_full_images': audit['shared_full_images_preserved_count'],
                'detection_records': audit['detection_records_count']
            }
        }
    else:
        summary = {
            'mode': 'C',
            'mode_title': 'Option C — Remove All Associated Data (Identity, Exclusive Images & History)',
            'will_delete': {
                'sface_embeddings': audit['sface_embeddings_count'],
                'legacy_dlib_encodings': audit['legacy_dlib_encodings_count'],
                'face_crop_images': audit['face_crop_images_count'],
                'exclusive_full_images': audit['exclusive_full_images_count'],
                'detection_records': audit['detection_records_count']
            },
            'will_preserve': {
                'shared_full_images': audit['shared_full_images_preserved_count']
            }
        }

    return jsonify({
        'person_id': person_id,
        'audit': audit,
        'preview': summary
    })

@app.route('/api/person/<person_id>/delete', methods=['POST'])
def delete_person(person_id):
    """
    Execute Option A, Option B, or Option C deletion safely with full audit reporting.
    """
    data = request.json or {}
    mode = (data.get('mode') or 'C').upper()
    confirmed = bool(data.get('confirm', False))

    if not confirmed:
        return jsonify({'error': 'Explicit confirmation (confirm: true) is required'}), 400
    if mode not in ('A', 'B', 'C'):
        return jsonify({'error': 'Mode must be A, B, or C'}), 400

    with state_lock:
        audit = analyze_person_storage_ownership(person_id)
        person_folder = os.path.join(FACES_FOLDER, person_id)

        if not os.path.exists(person_folder) and person_id not in identity_profiles and person_id not in known_face_encodings:
            return jsonify({'error': f'{person_id} not found'}), 404

        failed_items = []
        deleted_sface = 0
        deleted_dlib = 0
        deleted_face_crops = 0
        deleted_full_imgs = 0
        deleted_detections = 0

        # Step 1 (All Options A, B, C): Remove identity enrollment and facial embeddings
        if person_id in identity_profiles:
            deleted_sface = len(identity_profiles[person_id].get('sface', []))
            if mode == 'A':
                # Keep folder metadata as unenrolled so images remain browsable on dashboard, but clear all embeddings
                identity_profiles[person_id]['sface'] = []
                identity_profiles[person_id]['centroid'] = None
                identity_profiles[person_id]['enrolled'] = False
            else:
                del identity_profiles[person_id]

        if person_id in known_face_encodings:
            deleted_dlib = len(known_face_encodings[person_id])
            del known_face_encodings[person_id]

        # Step 2 (Options B & C): Delete exclusively owned images
        if mode in ('B', 'C'):
            for full_fname in audit['exclusive_full_filenames']:
                fpath = os.path.join(FULL_IMAGES_FOLDER, full_fname)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                        deleted_full_imgs += 1
                    except Exception as e:
                        failed_items.append({'path': fpath, 'error': str(e)})

            if os.path.exists(person_folder):
                for crop_path in glob.glob(os.path.join(person_folder, '*.jpg')):
                    try:
                        os.remove(crop_path)
                        deleted_face_crops += 1
                    except Exception as e:
                        failed_items.append({'path': crop_path, 'error': str(e)})
                try:
                    shutil.rmtree(person_folder)
                except Exception as e:
                    failed_items.append({'path': person_folder, 'error': str(e)})

        # Step 3 (Option C): Remove historical detection records and labels
        if mode == 'C':
            if person_id in person_detections:
                deleted_detections = len(person_detections[person_id])
                del person_detections[person_id]
            if person_id in person_labels:
                del person_labels[person_id]
                save_labels()

        # Persist all modified stores to disk so deletion survives restart
        save_profiles()
        save_encodings()
        save_detections()

        complete = (len(failed_items) == 0)
        return jsonify({
            'success': complete,
            'partial': not complete,
            'person_id': person_id,
            'mode': mode,
            'deleted': {
                'sface_embeddings': deleted_sface,
                'legacy_dlib_encodings': deleted_dlib,
                'face_crop_images': deleted_face_crops,
                'exclusive_full_images': deleted_full_imgs,
                'detection_records': deleted_detections
            },
            'preserved_shared_full_images': audit['shared_full_images_preserved_count'],
            'failed_items': failed_items
        })

@app.route('/api/detections/<person_id>')
def get_person_detections(person_id):
    hours = request.args.get('hours', default=24, type=int)
    data = get_bucketed_detections(person_id, hours)
    return jsonify(data)

@app.route('/image/<person_id>/<filename>')
def serve_image(person_id, filename):
    try:
        image_path = os.path.join(FACES_FOLDER, os.path.basename(person_id), os.path.basename(filename))
        if os.path.exists(image_path):
            return send_file(image_path, mimetype='image/jpeg')
        return "Image not found", 404
    except Exception as e:
        return str(e), 500

@app.route('/unrecognized_image/<filename>')
def serve_unrecognized_image(filename):
    try:
        image_path = os.path.join(UNRECOGNIZED_FOLDER, os.path.basename(filename))
        if os.path.exists(image_path):
            return send_file(image_path, mimetype='image/jpeg')
        return "Image not found", 404
    except Exception as e:
        return str(e), 500

@app.route('/api/camera/settings', methods=['GET'])
def get_camera_settings():
    """Fetch current camera hardware parameters from ESP32-CAM Port 80 (/status)"""
    cam_ip = get_current_camera_ip()
    target_url = f"http://{cam_ip}:80/status"
    try:
        req = urllib.request.Request(target_url, headers={'User-Agent': 'ESP32-Dashboard'})
        with urllib.request.urlopen(req, timeout=1.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                data = calibrate_esp32_temperature(data)
                with state_lock:
                    cached_camera_settings.update(data)
                    cached_camera_settings['online'] = True
                return jsonify({**cached_camera_settings, 'camera_ip': cam_ip})
        return jsonify({'status': 'error', 'online': False, 'camera_ip': cam_ip,
                        'error': f'Camera returned HTTP {resp.status}'}), 502
    except Exception as e:
        return jsonify({'status': 'error', 'online': False, 'camera_ip': cam_ip,
                        'error': f'Could not read camera settings: {e}'}), 502

CAMERA_DEFAULTS_FILE = os.path.join('data', 'camera_defaults.json')
CAMERA_SETTING_RANGES = {
    'framesize': {5, 6, 8, 9, 10, 11, 12, 13},
    'quality': range(10, 64),
    'flash': range(256),
    'brightness': range(-2, 3),
    'contrast': range(-2, 3),
    'saturation': range(-2, 3),
    'special_effect': range(7),
    'vflip': {0, 1},
    'hmirror': {0, 1},
    'awb': {0, 1},
    'aec': {0, 1},
}

def _load_camera_defaults():
    if os.path.exists(CAMERA_DEFAULTS_FILE):
        try:
            with open(CAMERA_DEFAULTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {
        'framesize': 9,
        'quality': 12,
        'flash': 0,
        'brightness': 0,
        'contrast': 0,
        'saturation': 0,
        'vflip': 0,
        'hmirror': 0,
        'awb': 1,
        'aec': 1,
    }

def _save_camera_defaults(defaults_dict):
    os.makedirs(os.path.dirname(CAMERA_DEFAULTS_FILE), exist_ok=True)
    with open(CAMERA_DEFAULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(defaults_dict, f, indent=2)

def _apply_camera_defaults(cam_ip=None):
    defaults = _load_camera_defaults()
    ip_addr = cam_ip or get_current_camera_ip()
    applied = {}
    for k, v in defaults.items():
        if k not in CAMERA_SETTING_RANGES:
            continue
        try:
            iv = int(v)
            if iv not in CAMERA_SETTING_RANGES[k]:
                continue
            url = f"http://{ip_addr}:80/control?var={urllib.parse.quote(str(k))}&val={iv}"
            req = urllib.request.Request(url, headers={'User-Agent': 'ESP32-Dashboard'})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                applied[k] = (resp.status == 200)
        except Exception:
            applied[k] = False
    if applied:
        refresh_camera_hardware_status()
    return applied

@app.route('/api/camera/defaults', methods=['GET', 'POST'])
def camera_defaults_api():
    """Get or update persisted default camera settings (applied immediately & on reconnect)"""
    if request.method == 'GET':
        return jsonify({'ok': True, 'defaults': _load_camera_defaults()})

    patch = request.get_json(silent=True) or {}
    current = _load_camera_defaults()
    for k, v in patch.items():
        if k in ('flash', 'led_intensity'):
            k = 'flash'
        if k not in CAMERA_SETTING_RANGES:
            return jsonify({'ok': False, 'error': f'Unsupported camera setting: {k}'}), 400
        try:
            iv = int(v)
        except (ValueError, TypeError):
            return jsonify({'ok': False, 'error': f'Invalid value for {k}'}), 400
        if iv not in CAMERA_SETTING_RANGES[k]:
            return jsonify({'ok': False, 'error': f'Out of range value for {k}: {iv}'}), 400
        current[k] = iv

    _save_camera_defaults(current)
    applied = _apply_camera_defaults()
    try:
        event_db.log_audit('user', 'camera_defaults_updated', 'camera_defaults.json', json.dumps(patch))
    except Exception:
        pass
    return jsonify({'ok': True, 'defaults': current, 'applied': applied})

@app.route('/api/camera/settings', methods=['POST'])
def set_camera_setting():
    """Send parameter adjustment to ESP32-CAM Port 80 (/control?var=...&val=...)"""
    req_data = request.get_json(silent=True) or {}
    var_name = req_data.get('var')
    val = req_data.get('val')
    if var_name is None or val is None:
        return jsonify({'error': 'Missing var or val parameter'}), 400

    try:
        val = int(val)
    except (ValueError, TypeError):
        return jsonify({'error': 'Value must be an integer'}), 400

    if var_name in ('flash', 'led_intensity'):
        var_name = 'flash'

    setting_ranges = {
        'framesize': {5, 6, 8, 9, 10, 11, 12, 13},
        'quality': range(10, 64),
        'flash': range(256),
        'brightness': range(-2, 3),
        'contrast': range(-2, 3),
        'saturation': range(-2, 3),
        'special_effect': range(7),
        'vflip': {0, 1},
        'hmirror': {0, 1},
        'awb': {0, 1},
        'aec': {0, 1},
    }
    if not isinstance(var_name, str) or var_name not in setting_ranges:
        return jsonify({'error': f'Unsupported camera setting: {var_name}'}), 400
    if val not in setting_ranges[var_name]:
        return jsonify({'error': f'Invalid value for {var_name}: {val}'}), 400

    cam_ip = get_current_camera_ip()
    target_url = f"http://{cam_ip}:80/control?var={urllib.parse.quote(str(var_name))}&val={val}"

    try:
        req = urllib.request.Request(target_url, headers={'User-Agent': 'ESP32-Dashboard'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status != 200:
                return jsonify({'status': 'error', 'var': var_name,
                                'error': f'Camera control returned HTTP {resp.status}'}), 502

        status_req = urllib.request.Request(
            f"http://{cam_ip}:80/status",
            headers={'User-Agent': 'ESP32-Dashboard'})
        with urllib.request.urlopen(status_req, timeout=2.5) as resp:
            if resp.status != 200:
                return jsonify({'status': 'error', 'var': var_name,
                                'error': f'Camera read-back returned HTTP {resp.status}'}), 502
            actual = json.loads(resp.read().decode('utf-8')).get(var_name)
        if actual is None or int(actual) != val:
            return jsonify({'status': 'error', 'var': var_name, 'requested': val,
                            'actual': actual, 'error': 'Camera did not apply the requested setting'}), 502

        with state_lock:
            cached_camera_settings[var_name] = int(actual)
            cached_camera_settings['online'] = True
        event_db.log_audit("user", "camera_setting", var_name, f"val={int(actual)}")
        return jsonify({'status': 'ok', 'var': var_name, 'val': val, 'actual': int(actual)})
    except Exception as e:
        with state_lock:
            cached_camera_settings['online'] = False
        return jsonify({'status': 'error', 'var': var_name,
                        'error': f'Camera setting was not confirmed: {e}'}), 502

@app.route('/api/camera/sensor', methods=['GET', 'POST'])
def api_camera_sensor():
    """Get or toggle ESP32-CAM optical sensor power (low-power standby vs active)."""
    cam_ip = get_current_camera_ip()
    if request.method == 'GET':
        with state_lock:
            cam_info = cached_camera_settings.get('camera', {})
            sensor_active = bool(cam_info.get('sensor_active', camera_online)) if isinstance(cam_info, dict) else camera_online
        return jsonify({
            'ok': True,
            'sensor_active': sensor_active,
            'camera_online': camera_online,
            'recording_active': door_recording_active,
            'recording_path': door_recording_path,
        })

    req_data = request.get_json(silent=True) or {}
    active_val = req_data.get('active', 'toggle')
    if active_val is True or str(active_val).lower() in ('on', '1', 'true'):
        state_param = 'on'
    elif active_val is False or str(active_val).lower() in ('off', '0', 'false'):
        state_param = 'off'
    else:
        state_param = 'toggle'

    cam_ips = []
    if active_camera_ip:
        cam_ips.append(active_camera_ip)
    for ip in CANDIDATE_IPS:
        if ip and ip not in cam_ips:
            cam_ips.append(ip)
    if not cam_ips:
        cam_ips = ['192.168.29.10']

    last_err = 'No camera IP reachable'
    for cam_ip in cam_ips:
        target_url = f"http://{cam_ip}:80/api/v1/camera/power?state={state_param}"
        try:
            req = urllib.request.Request(target_url, headers={'User-Agent': 'ESP32-Dashboard'})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                payload = json.loads(resp.read().decode('utf-8', errors='ignore'))
            if payload.get('sensor_active'):
                stream_wake_event.set()
            threading.Thread(target=refresh_camera_hardware_status, daemon=True).start()
            return jsonify(payload)
        except Exception as e:
            last_err = str(e)
            continue
    return jsonify({'ok': False, 'error': f"ESP32-CAM unreachable ({last_err}). Ensure latest firmware is flashed."}), 502

@app.route('/api/v1/event', methods=['POST'])
def api_v1_event_push():
    """Fast HTTP push endpoint for DOOR_OPEN / DOOR_CLOSED recording triggers."""
    data = request.get_json(silent=True) or {}
    ev_name = str(data.get('event') or request.get_data(as_text=True) or '').upper()
    if 'DOOR_OPEN' in ev_name or 'MOTION_START' in ev_name:
        start_door_recording(duration_s=20.0)
        return jsonify({'ok': True, 'recording_armed': True, 'event': 'DOOR_OPEN'})
    if 'DOOR_CLOSED' in ev_name:
        stop_door_recording_tail(tail_s=5.0)
        return jsonify({'ok': True, 'recording_tail_s': 5.0, 'event': 'DOOR_CLOSED'})
    return jsonify({'ok': True, 'event': ev_name})

@app.route('/api/testing/simulate', methods=['POST'])
def api_testing_simulate():
    """Run real hardware/server simulations from the Dashboard Testing tab."""
    global active_camera_ip
    import base64
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip()
    cam_ip = active_camera_ip or (CANDIDATE_IPS[0] if CANDIDATE_IPS else '192.168.29.10')
    wroom_ip = WROOM_NODE_IP
    t0 = time.time()

    def _http_call(url, method='GET', payload=None, timeout=3.0):
        req_data = json.dumps(payload).encode('utf-8') if payload is not None else None
        headers = {'User-Agent': 'Sentinel-Testing'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()

    try:
        if action in ('door_open', 'door_closed'):
            ev = 'DOOR_OPEN' if action == 'door_open' else 'DOOR_CLOSED'
            if ev == 'DOOR_OPEN':
                start_door_recording(duration_s=20.0)
            else:
                stop_door_recording_tail(tail_s=5.0)
            status, raw = _http_call(f"http://{cam_ip}:80/api/v1/event", method='POST',
                                     payload={'event': ev, 'src': 'SIM', 'val': 1}, timeout=4.0)
            return jsonify({'ok': True, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1),
                            'http_status': status, 'cam_response': json.loads(raw.decode('utf-8', 'ignore'))})

        if action in ('low_power_on', 'low_power_off'):
            state = 'off' if action == 'low_power_on' else 'on'
            status, raw = _http_call(f"http://{cam_ip}:80/api/v1/camera/power?state={state}", timeout=4.0)
            res = json.loads(raw.decode('utf-8', 'ignore'))
            if res.get('sensor_active'):
                stream_wake_event.set()
            return jsonify({'ok': bool(res.get('ok', True)), 'action': action,
                            'latency_ms': round((time.time() - t0) * 1000, 1), 'cam_response': res})

        if action == 'udp_record_trigger':
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b'{"event":"DOOR_OPEN","src":"SIM_UDP"}', ('127.0.0.1', 5055))
            sock.close()
            time.sleep(0.05)
            return jsonify({'ok': True, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1),
                            'recording_active': door_recording_active})

        if action == 'ping_nodes':
            out = {}
            for name, ip in (('cam', cam_ip), ('wroom', wroom_ip)):
                nt0 = time.time()
                try:
                    st, body = _http_call(f"http://{ip}:80/ping", timeout=2.0)
                    out[name] = {'ip': ip, 'online': st == 200, 'rtt_ms': round((time.time() - nt0) * 1000, 1),
                                 'body': body.decode('utf-8', 'ignore')}
                except Exception as e:
                    out[name] = {'ip': ip, 'online': False, 'error': str(e)}
            return jsonify({'ok': True, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1), 'nodes': out})

        if action in ('cam_events', 'wroom_events'):
            ip = cam_ip if action == 'cam_events' else wroom_ip
            st, body = _http_call(f"http://{ip}:80/events", timeout=2.5)
            return jsonify({'ok': st == 200, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1),
                            'data': json.loads(body.decode('utf-8', 'ignore'))})

        if action == 'cam_spiffs_logs':
            st, body = _http_call(f"http://{cam_ip}:80/logs", timeout=2.5)
            return jsonify({'ok': st == 200, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1),
                            'logs': body.decode('utf-8', 'ignore')})

        if action in ('cam_snapshot', 'wroom_saved_image'):
            url = f"http://{cam_ip}:80/capture" if action == 'cam_snapshot' else f"http://{wroom_ip}:80/api/v1/image"
            st, img_bytes = _http_call(url, timeout=4.0)
            b64 = base64.b64encode(img_bytes).decode('ascii')
            return jsonify({'ok': st == 200, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1),
                            'bytes': len(img_bytes), 'image_data_url': f"data:image/jpeg;base64,{b64}"})

        if action == 'connect_fallback_ap':
            import subprocess, tempfile
            ssid = 'Sentinel-CAM'
            xml = (
                '<?xml version="1.0"?>'
                '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
                f'<name>{ssid}</name><SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>'
                '<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>'
                '<MSM><security><authEncryption><authentication>WPA2PSK</authentication>'
                '<encryption>AES</encryption><useOneX>false</useOneX></authEncryption>'
                '<sharedKey><keyType>passPhrase</keyType><protected>false</protected>'
                '<keyMaterial>sentinel123</keyMaterial></sharedKey></security></MSM></WLANProfile>'
            )
            with tempfile.NamedTemporaryFile('w', suffix='.xml', delete=False, encoding='utf-8') as tf:
                tf.write(xml)
                xml_path = tf.name
            try:
                subprocess.run(['netsh', 'wlan', 'add', 'profile', f'filename={xml_path}'], capture_output=True, text=True, timeout=5)
                subprocess.run(['netsh', 'wlan', 'disconnect'], capture_output=True, text=True, timeout=5)
                res = subprocess.run(['netsh', 'wlan', 'connect', f'name={ssid}', f'ssid={ssid}'], capture_output=True, text=True, timeout=8)
                active_camera_ip = '192.168.4.1'
                return jsonify({'ok': res.returncode == 0, 'action': action, 'ap_ip': '192.168.4.1',
                                'stdout': res.stdout.strip(), 'stderr': res.stderr.strip(),
                                'latency_ms': round((time.time() - t0) * 1000, 1)})
            finally:
                try:
                    os.remove(xml_path)
                except Exception:
                    pass

        return jsonify({'ok': False, 'error': f'Unknown simulation action: {action}'}), 400
    except Exception as e:
        return jsonify({'ok': False, 'action': action, 'latency_ms': round((time.time() - t0) * 1000, 1), 'error': str(e)}), 502

@app.route('/upload', methods=['POST'])
def upload_image():
    """HTTP POST endpoint for direct frame upload compatibility"""
    try:
        image_data = request.data
        if not image_data:
            return "No image data received", 400
        process_frame(image_data)
        return "Frame processed", 200
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route('/api/v1/analyze', methods=['POST'])
def analyze_frame_api():
    """Phase 7 standardized HTTP API for ESP32-CAM frame analysis"""
    try:
        image_data = request.data
        if not image_data:
            return jsonify({'ok': False, 'error': 'No image data'}), 400
        
        device_id = request.headers.get('X-Device-ID', 'unknown')
        event_type = request.headers.get('X-Event-Type', 'UNKNOWN')
        
        if not is_recognition_active:
            return jsonify({
                'ok': True,
                'device': device_id,
                'event': event_type,
                'recognition_active': False,
                'face_detected': False,
                'message': 'Recognition pipeline is currently disabled'
            }), 200
            
        process_frame(image_data)
        return jsonify({
            'ok': True,
            'device': device_id,
            'event': event_type,
            'recognition_active': True,
            'status': 'processed'
        }), 200
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/v1/vlm/status', methods=['GET'])
def vlm_status_api():
    """Return runtime diagnostics for the DirectML VLM engine"""
    return jsonify({'ok': True, 'vlm': vlm.get_status()})

@app.route('/api/v1/vlm/toggle', methods=['POST'])
def vlm_toggle_api():
    """Toggle VLM Universal Recognition Engine ON/OFF"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        if 'enabled' in data:
            vlm.enabled = bool(data['enabled'])
        else:
            vlm.enabled = not getattr(vlm, 'enabled', True)
        return jsonify({'ok': True, 'vlm_enabled': vlm.enabled})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/v1/vlm/describe', methods=['POST'])
def vlm_describe_api():
    """Invoke VLM on the uploaded frame or latest capture"""
    try:
        image_data = request.data
        if not image_data:
            with latest_jpeg_lock:
                image_data = latest_annotated_jpeg
        if not image_data:
            return jsonify({'ok': False, 'error': 'No image data available'}), 400
        
        caption = vlm.describe_image(image_data)
        return jsonify({
            'ok': True,
            'caption': caption,
            'provider': vlm.active_provider,
            'gpu_accelerated': "DirectML" in vlm.active_provider or "CUDA" in vlm.active_provider
        }), 200
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

WROOM_NODE_IP = "192.168.29.20"

def _trigger_esp_reboot(ip_addr):
    """Send /reboot request to an ESP32 node (CAM or WROOM)."""
    try:
        req = urllib.request.Request(f"http://{ip_addr}:80/reboot", headers={'User-Agent': 'ESP32-Dashboard'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return {'ok': resp.status == 200, 'ip': ip_addr, 'status': resp.status}
    except Exception as e:
        return {'ok': False, 'ip': ip_addr, 'error': str(e)}

@app.route('/api/system/reboot_cam', methods=['POST'])
def reboot_cam_api():
    """Reboot the ESP32-CAM node"""
    cam_ip = get_current_camera_ip()
    res = _trigger_esp_reboot(cam_ip)
    event_db.log_audit("user", "reboot_cam", cam_ip, f"ok={res.get('ok')}")
    return jsonify(res), (200 if res.get('ok') else 502)

@app.route('/api/system/reboot_wroom', methods=['POST'])
def reboot_wroom_api():
    """Reboot the ESP32-WROOM node"""
    res = _trigger_esp_reboot(WROOM_NODE_IP)
    event_db.log_audit("user", "reboot_wroom", WROOM_NODE_IP, f"ok={res.get('ok')}")
    return jsonify(res), (200 if res.get('ok') else 502)

@app.route('/api/system/full_reboot', methods=['POST'])
def full_reboot_api():
    """Reboot CAM + WROOM, reload Face Recognition & Object Detection models, and clear stale buffers"""
    cam_ip = get_current_camera_ip()
    cam_res = _trigger_esp_reboot(cam_ip)
    wroom_res = _trigger_esp_reboot(WROOM_NODE_IP)

    # Clear queued frames and reload both AI engines
    while not frame_queue.empty():
        try:
            frame_queue.get_nowait()
        except queue.Empty:
            break

    face_reloaded = False
    obj_reloaded = False
    try:
        init_cv_models()
        face_reloaded = True
    except Exception as e:
        print(f"[!] Face model reload failed: {e}")
    try:
        obj_reloaded = bool(vlm.reload())
    except Exception as e:
        print(f"[!] Object detector reload failed: {e}")

    event_db.log_audit("user", "full_reboot", f"{cam_ip},{WROOM_NODE_IP}",
                       f"face_reloaded={face_reloaded}, obj_reloaded={obj_reloaded}")

    return jsonify({
        'ok': True,
        'cam': cam_res,
        'wroom': wroom_res,
        'face_models_reloaded': face_reloaded,
        'object_model_reloaded': obj_reloaded
    }), 200

_wroom_health_cache = {'ts': 0.0, 'online': False, 'info': {}}

def _probe_wroom_health(force=False):
    now = time.time()
    if not force and (now - _wroom_health_cache['ts']) < 5.0:
        return _wroom_health_cache['online'], _wroom_health_cache['info']
    online = False
    info = {}
    try:
        req = urllib.request.Request(f"http://{WROOM_NODE_IP}:80/status", headers={'User-Agent': 'Sentinel-Dashboard'})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            online = resp.status == 200
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                info = json.loads(raw)
            except Exception:
                info = {'raw': raw[:120]}
    except Exception:
        try:
            import socket
            with socket.create_connection((WROOM_NODE_IP, 80), timeout=0.45):
                online = True
        except Exception:
            online = False
    _wroom_health_cache['ts'] = now
    _wroom_health_cache['online'] = online
    _wroom_health_cache['info'] = info
    return online, info

@app.route('/api/v1/devices/health', methods=['GET'])
def devices_health_api():
    """Unified health telemetry for ESP32-WROOM, ESP32-CAM, AI Server, and EventDB"""
    force = request.args.get('refresh') == '1'
    wroom_online, wroom_info = _probe_wroom_health(force=force)
    if force:
        refresh_camera_hardware_status()
    now = time.time()
    cam_alive = camera_online and (now - last_frame_received_time < 12.0)
    with state_lock:
        cam_info = cached_camera_settings.get('camera', {})
        sensor_ready = bool(cam_info.get('ready', False)) if isinstance(cam_info, dict) else False
        sensor_active = bool(cam_info.get('sensor_active', cam_alive)) if isinstance(cam_info, dict) else cam_alive
        cam_board_online = bool(cached_camera_settings.get('online', False)) or cam_alive
        temp_c = cached_camera_settings.get('temp_c')
        recog_on = recognition_enabled
    notif_status = notification_manager.get_status()
    mqtt_cfg = notification_manager.config_store.get().get('providers', {}).get('mqtt', {})
    return jsonify({
        'ok': True,
        'wroom': {
            'online': wroom_online,
            'ip': WROOM_NODE_IP,
            'info': wroom_info,
        },
        'camera': {
            'online': cam_alive,
            'board_online': cam_board_online,
            'sensor_ready': sensor_ready,
            'sensor_active': sensor_active,
            'ip': active_camera_ip if cam_alive else (CANDIDATE_IPS[0] if CANDIDATE_IPS else '192.168.29.10'),
            'fps': round(stream_fps, 1) if cam_alive else 0.0,
            'temp_c': temp_c,
        },
        'ai_server': {
            'online': True,
            'recognition_enabled': recog_on,
            'vlm': vlm.get_status(),
        },
        'database': {
            'online': True,
            'path': 'data/events.db',
        },
        'mqtt': {
            'enabled': bool(mqtt_cfg.get('enabled', False)),
            'last_error': notif_status.get('providers', {}).get('mqtt', {}).get('last_error'),
        }
    })

@app.route('/api/v1/events/db', methods=['GET'])
def events_db_api():
    """Phase 9: Query persistent SQLite recognition & system events"""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    event_type = request.args.get('type', 'all')
    person_id = request.args.get('person_id')
    search = request.args.get('q')
    return jsonify({'ok': True, **event_db.query_events(limit=limit, offset=offset, event_type=event_type, person_id=person_id, search=search)})

@app.route('/api/v1/audit', methods=['GET'])
def audit_log_api():
    """Phase 9: Query persistent user/system audit log"""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    return jsonify({'ok': True, **event_db.query_audit(limit=limit, offset=offset)})

@app.route('/api/v1/analytics', methods=['GET'])
def analytics_db_api():
    """Phase 9: Aggregated 24h hourly, multi-day daily, and top-visitor analytics"""
    days = request.args.get('days', 7, type=int)
    return jsonify({'ok': True, **event_db.get_analytics(days=days)})

# ============================================================================
# PHASE 10: NOTIFICATION & ALERT SYSTEM ROUTES
# ============================================================================
@app.route('/api/v1/event', methods=['POST'])
def ingest_hardware_event_api():
    """Phase 10: Unified event ingestion from ESP32-WROOM / ESP32-CAM / services"""
    data = request.get_json(silent=True) or {}
    event_type = str(data.get('event_type') or 'SYSTEM_FAILURE').strip().upper()
    title = str(data.get('title') or event_type.replace('_', ' ').title())
    message = str(data.get('message') or f"{event_type} event received.")
    severity = str(data.get('severity') or ('critical' if event_type in ('CAMERA_OFFLINE', 'SYSTEM_FAILURE') else 'info')).lower()
    source = str(data.get('source') or 'esp32')
    meta = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    meta['source'] = source

    snap_bytes = None
    if data.get('attach_snapshot', True):
        with latest_jpeg_lock:
            snap_bytes = latest_jpeg

    try:
        event_db.log_audit(source, event_type.lower(), target=title, details=message)
    except Exception:
        pass

    ev = NotificationEvent(
        event_type=event_type,
        title=title,
        message=message,
        severity=severity,
        person_id=data.get('person_id'),
        person_label=data.get('person_label'),
        image_bytes=snap_bytes,
        metadata=meta,
        dedup_key=data.get('dedup_key'),
    )
    accepted, reason = notification_manager.emit(ev, bypass_dedup=bool(data.get('bypass_dedup', False)))
    return jsonify({'ok': True, 'accepted': accepted, 'reason': reason, 'event_id': ev.event_id}), 200

@app.route('/api/v1/notifications/status', methods=['GET'])
def notifications_status_api():
    """Phase 10: Provider health, queue metrics, rules, and recent alerts"""
    return jsonify({'ok': True, **notification_manager.get_status()})

@app.route('/api/v1/notifications/config', methods=['GET', 'POST'])
def notifications_config_api():
    """Phase 10: Read or update backend-only notification provider & rule config (secrets masked on GET)"""
    if request.method == 'GET':
        return jsonify({'ok': True, 'config': mask_notification_secrets(notification_manager.config_store.get())})
    patch = strip_masked_secrets(request.get_json(silent=True) or {})
    updated = notification_manager.config_store.update(patch)
    try:
        event_db.log_audit('dashboard', 'notifications_config_updated', target='notification_config', details='Updated provider/rule config')
    except Exception:
        pass
    return jsonify({'ok': True, 'config': mask_notification_secrets(updated)})

@app.route('/api/v1/notifications/test', methods=['POST'])
def notifications_test_api():
    """Phase 10: Trigger a synchronous test notification (all routed providers or a specific provider)"""
    data = request.get_json(silent=True) or {}
    event_type = str(data.get('event_type') or 'SYSTEM_FAILURE').strip().upper()
    provider = data.get('provider')
    title = str(data.get('title') or f"Sentinel Test Alert ({provider or event_type})")
    message = str(data.get('message') or "Test notification triggered from Sentinel Notification Center.")
    severity = str(data.get('severity') or 'info')

    snap_bytes = None
    with latest_jpeg_lock:
        snap_bytes = latest_jpeg

    ev = NotificationEvent(
        event_type=event_type,
        title=title,
        message=message,
        severity=severity,
        image_bytes=snap_bytes,
        metadata={'test': True},
    )
    force = [provider] if provider else None
    results = notification_manager.dispatch_sync(ev, force_providers=force, bypass_cooldown=True)
    return jsonify({'ok': True, 'event_id': ev.event_id, 'results': results}), 200

@app.route('/reset', methods=['POST'])
def reset():
    """Reset all data"""
    with state_lock:
        if os.path.exists(DATA_FOLDER):
            shutil.rmtree(DATA_FOLDER, ignore_errors=True)
        for folder in [DATA_FOLDER, BACKUP_FOLDER, FULL_IMAGES_FOLDER, FACES_FOLDER, UNRECOGNIZED_FOLDER]:
            os.makedirs(folder, exist_ok=True)
        known_face_encodings.clear()
        identity_profiles.clear()
        person_detections.clear()
        person_labels.clear()
        pending_candidate_tracks.clear()
        save_encodings()
        save_profiles()
        save_detections()
        save_labels()
    event_db.clear_all()
    return "All data reset", 200

# ============================================================================
# PHASE 11 & 12: INDUSTRIAL SECURITY & READINESS ENDPOINTS
# ============================================================================
@app.route('/login', methods=['GET', 'POST'])
def web_login_page():
    """Browser auth page backed by ADmin / user credentials in secrets.h"""
    if request.method == 'GET':
        if session.get('sentinel_auth') is True:
            return redirect('/')
        return render_template('login.html', error=None)

    data = request.get_json(silent=True)
    if isinstance(data, dict):
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '')
    else:
        username = str(request.form.get('username') or '').strip()
        password = str(request.form.get('password') or '')

    client_ip = request.remote_addr or 'unknown'
    ok, role = verify_secrets_h_login(username, password)
    if ok:
        session['sentinel_auth'] = True
        session['auth_role'] = role
        session['auth_user'] = username
        event_db.log_audit(username, 'login_success', client_ip, f'role={role}')
        if isinstance(data, dict):
            return jsonify({'ok': True, 'role': role, 'user': username}), 200
        return redirect('/')

    event_db.log_audit(username or 'unknown', 'login_failed', client_ip, 'invalid credentials')
    if isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401
    return render_template('login.html', error='Invalid username or password'), 401

@app.route('/logout', methods=['GET', 'POST'])
def web_logout_page():
    user = session.pop('auth_user', None)
    session.pop('auth_role', None)
    session.pop('sentinel_auth', None)
    if user:
        event_db.log_audit(user, 'logout', request.remote_addr or 'unknown', '')
    return redirect('/login')

@app.route('/api/v1/security/status', methods=['GET'])
def security_status_api():
    """Phase 11: Security posture (headers, rate limiter, PIN lock, API key preview)"""
    return jsonify({'ok': True, **security_guard.get_public_status()})

@app.route('/api/v1/security/pin', methods=['POST'])
def security_set_pin_api():
    """Set/clear Admin PIN and optional localhost lock"""
    data = request.get_json(silent=True) or {}
    if 'pin' in data:
        pin = str(data.get('pin') or '').strip()
        if pin and len(pin) < 4:
            return jsonify({'ok': False, 'error': 'PIN must be at least 4 characters'}), 400
        security_guard.set_pin(pin)
        event_db.log_audit('user', 'security_pin_updated', 'security_config', 'PIN configured' if pin else 'PIN cleared')
    if 'lock_localhost' in data:
        try:
            security_guard.set_lock_localhost(bool(data.get('lock_localhost')))
            event_db.log_audit('user', 'security_lock_toggled', 'security_config', f"lock_localhost={bool(data.get('lock_localhost'))}")
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({'ok': True, **security_guard.get_public_status()})

@app.route('/api/v1/security/login', methods=['POST'])
def security_login_api():
    """Authenticate session via Admin PIN (rate-limited + brute-force lockout)"""
    from flask import session
    data = request.get_json(silent=True) or {}
    pin = str(data.get('pin') or '')
    ok, msg = security_guard.check_pin(pin, request.remote_addr or 'unknown')
    if not ok:
        event_db.log_audit('security', 'login_failed', request.remote_addr or 'unknown', msg)
        if 'Locked' in msg:
            notification_manager.emit(NotificationEvent(
                event_type='SYSTEM_FAILURE',
                title='Security Lockout Triggered',
                message=f"Brute-force PIN attempts from {request.remote_addr}: {msg}",
                severity='critical',
                dedup_key=f"BRUTEFORCE:{request.remote_addr}",
            ))
        return jsonify({'ok': False, 'error': msg}), 401
    session['sentinel_auth'] = True
    event_db.log_audit('security', 'login_success', request.remote_addr or 'unknown', 'Session authenticated')
    return jsonify({'ok': True, 'message': 'Authenticated'})

@app.route('/api/v1/security/logout', methods=['POST'])
def security_logout_api():
    from flask import session
    session.pop('sentinel_auth', None)
    return jsonify({'ok': True})

@app.route('/api/v1/security/rotate-key', methods=['POST'])
def security_rotate_key_api():
    new_key = security_guard.rotate_api_key()
    event_db.log_audit('user', 'api_key_rotated', 'security_config', 'Rotated X-Sentinel-API-Key')
    return jsonify({'ok': True, 'api_key': new_key, **security_guard.get_public_status()})

hw_telemetry = {'cpu_pct': 0.0, 'gpu_pct': 0.0, 'npu_pct': 0.0}
_hw_telemetry_proc = None

def _stop_hw_telemetry():
    global _hw_telemetry_proc
    if _hw_telemetry_proc is not None:
        try:
            _hw_telemetry_proc.terminate()
        except Exception:
            pass
        _hw_telemetry_proc = None

atexit.register(_stop_hw_telemetry)

def _hw_telemetry_sampler_thread():
    """Sample real OS-wide CPU, Windows GPU, and Intel/AMD NPU utilization every 1s."""
    global _hw_telemetry_proc
    import subprocess, re
    if psutil is not None:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    def _cpu_loop():
        while stream_active:
            if psutil is not None:
                try:
                    hw_telemetry['cpu_pct'] = round(psutil.cpu_percent(interval=1.0), 1)
                except Exception:
                    time.sleep(1.0)
            else:
                time.sleep(1.0)

    threading.Thread(target=_cpu_loop, daemon=True).start()

    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    while stream_active:
        try:
            _hw_telemetry_proc = subprocess.Popen(
                ['typeperf', r'\GPU Engine(*)\Utilization Percentage', '-si', '1'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                creationflags=creationflags
            )
            header = None
            gpu_luids = set()
            col_meta = []
            for raw_line in _hw_telemetry_proc.stdout:
                if not stream_active:
                    break
                line = raw_line.strip()
                if not line or line.startswith('(') or line.startswith('Exiting'):
                    continue
                parts = [p.strip('"') for p in line.split(',')]
                if header is None:
                    header = parts
                    for idx, col in enumerate(header[1:], start=1):
                        m = re.search(r'luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+).*?engtype_([^)\\]+)', col)
                        if m:
                            luid, eng = m.group(1), m.group(2).strip().lower()
                            if eng == '3d':
                                gpu_luids.add(luid)
                            col_meta.append((idx, luid, eng))
                    continue
                if len(parts) < 2:
                    continue
                gpu_eng_sums = {}
                npu_sum = 0.0
                for idx, luid, eng in col_meta:
                    if idx >= len(parts):
                        continue
                    try:
                        val = float(parts[idx])
                    except ValueError:
                        continue
                    if val <= 0.0:
                        continue
                    if luid in gpu_luids:
                        gpu_eng_sums[eng] = gpu_eng_sums.get(eng, 0.0) + val
                    elif 'compute' in eng:
                        npu_sum += val
                hw_telemetry['gpu_pct'] = round(min(100.0, max(gpu_eng_sums.values()) if gpu_eng_sums else 0.0), 1)
                hw_telemetry['npu_pct'] = round(min(100.0, npu_sum), 1)
        except Exception:
            time.sleep(3.0)
        finally:
            _stop_hw_telemetry()

threading.Thread(target=_hw_telemetry_sampler_thread, daemon=True).start()

@app.route('/api/v1/system/healthz', methods=['GET'])
def system_healthz_api():
    """Phase 12: Industrial readiness, thread liveness, memory & disk telemetry"""
    disk = shutil.disk_usage('.')
    rss_mb = None
    cpu_pct = hw_telemetry.get('cpu_pct')
    if psutil is not None:
        try:
            proc = psutil.Process(os.getpid())
            rss_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
            if not cpu_pct:
                cpu_pct = round(psutil.cpu_percent(interval=None), 1)
        except Exception:
            pass
    threads = {
        'mjpeg_consumer': bool(mjpeg_thread.is_alive()) if 'mjpeg_thread' in globals() else False,
        'ai_worker': bool(ai_thread.is_alive()) if 'ai_thread' in globals() else False,
        'backup_daemon': bool(backup_daemon.is_alive()) if 'backup_daemon' in globals() else False,
        'notification_worker': bool(notification_manager._worker.is_alive()) if getattr(notification_manager, '_worker', None) else False,
    }
    return jsonify({
        'ok': True,
        'uptime_s': round(time.time() - SERVER_START_EPOCH, 1),
        'threads': threads,
        'disk_free_gb': round(disk.free / (1024 ** 3), 2),
        'process_rss_mb': rss_mb,
        'cpu_pct': cpu_pct,
        'gpu_pct': hw_telemetry.get('gpu_pct', 0.0),
        'npu_pct': hw_telemetry.get('npu_pct', 0.0),
        'vlm': vlm.get_status(),
        'security': security_guard.get_public_status(),
    })

MODEL_CATALOG = {
    'object_detection': [
        {
            'id': 'yolov8n',
            'name': 'YOLOv8 Nano',
            'path': os.path.join(MODELS_FOLDER, 'yolov8n.onnx'),
            'weight_tier': 'light',
            'ram_est': '~350 MB',
            'description': 'Ultra-lightweight nano object detector for fast person/parcel detection.',
            'usage': 'Best for 8 GB RAM PCs, low battery drain, or high-FPS streams (4–8 ms/frame).',
        },
        {
            'id': 'yolo11s',
            'name': 'YOLO11 Small',
            'path': os.path.join(MODELS_FOLDER, 'yolo11s.onnx'),
            'weight_tier': 'medium',
            'ram_est': '~750 MB',
            'description': 'Balanced precision & speed detector used as the default Sentinel engine.',
            'usage': 'Great all-rounder for 16 GB+ PCs and NPU/iGPU offload (12–20 ms/frame).',
        },
        {
            'id': 'yolo11m',
            'name': 'YOLO11 Medium',
            'path': os.path.join(MODELS_FOLDER, 'yolo11m.onnx'),
            'weight_tier': 'heavy',
            'ram_est': '~1.5 GB',
            'description': 'High-accuracy medium detector for distant objects & low-light doors.',
            'usage': 'Recommended for 16 GB (warning) or 24 GB+ systems (22–38 ms/frame).',
        },
        {
            'id': 'yolo11l',
            'name': 'YOLO11 Large',
            'path': os.path.join(MODELS_FOLDER, 'yolo11l.onnx'),
            'weight_tier': 'heaviest',
            'ram_est': '~2.4 GB',
            'description': 'Maximum-precision flagship detector with heavy compute & memory footprint.',
            'usage': 'Best on 32 GB+ workstations or dedicated GPU/NPU INT8 (40–70 ms/frame).',
        },
    ],
    'face_recognition': [
        {
            'id': 'mobilefacenet',
            'name': 'InsightFace MobileFaceNet',
            'path': os.path.join(MODELS_FOLDER, 'insightface', 'recognition', 'model.onnx'),
            'weight_tier': 'light',
            'ram_est': '~250 MB',
            'description': 'Compact 13 MB face embedding model tuned for low-RAM & NPU efficiency.',
            'usage': 'Ideal for 8 GB RAM laptops and maximum battery life.',
        },
        {
            'id': 'sface',
            'name': 'SFace 128-D',
            'path': os.path.join(MODELS_FOLDER, 'face_recognition_sface_2021dec.onnx'),
            'weight_tier': 'light',
            'ram_est': '~450 MB',
            'description': 'Fast 128-dimensional OpenCV/ONNX face recognizer.',
            'usage': 'Reliable lightweight recognizer for 8–16 GB systems.',
        },
        {
            'id': 'buffalo_m_r50',
            'name': 'InsightFace ResNet50 (buffalo_m)',
            'path': os.path.join(MODELS_FOLDER, 'insightface_m', 'recognition', 'model.onnx'),
            'weight_tier': 'medium',
            'ram_est': '~1.2 GB',
            'description': 'High-accuracy 512-D ArcFace ResNet50 model for strict identity matching.',
            'usage': 'Default high-accuracy recognizer; smooth on 16 GB+ systems, heavy on <=8 GB.',
        },
    ],
}

def _evaluate_ram_zone(weight_tier, total_ram_gb):
    """Classify model into safe/optimal/warning/danger based on TOTAL physical RAM (accounting for iGPU UMA reserve)."""
    if total_ram_gb < 12.5:
        # <= 8 GB / 12 GB systems (<16 GB)
        if weight_tier in ('heavy', 'heaviest'):
            return 'danger', f"DANGER (<16 GB RAM): {total_ram_gb} GB usable RAM — high risk of severe lag or OOM."
        if weight_tier == 'medium':
            return 'warning', f"WARNING (<16 GB RAM): {total_ram_gb} GB usable RAM — may lag under load."
        return 'safe', "Safe for <=8–12 GB RAM systems."
    if total_ram_gb < 21.5:
        # 16 GB tier (13.5-16 GB OS-visible after iGPU UMA reserve)
        if weight_tier == 'heaviest':
            return 'danger', f"DANGER (16 GB Tier): Exceeds comfortable 16 GB RAM headroom."
        if weight_tier == 'heavy':
            return 'warning', f"WARNING (16 GB Tier): Heavy RAM/thermal load on 16 GB."
        return 'safe', "Cleared for 16 GB RAM."
    if total_ram_gb < 29.5:
        # 24 GB tier
        if weight_tier == 'heaviest':
            return 'warning', f"WARNING (24 GB Tier): High memory & thermal load on 24 GB."
        return 'safe', "Cleared for 24 GB RAM."
    # 32 GB+ Workstation tier
    return 'optimal', "32 GB+ Tier: All models cleared for maximum performance."

@app.route('/api/v1/models/catalog', methods=['GET'])
def api_models_catalog():
    import onnxruntime as ort
    from npu import get_accelerator_summary
    total_ram_gb = 16.0
    if psutil is not None:
        try:
            total_ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        except Exception:
            pass
    if total_ram_gb < 12.5:
        ram_tier = '< 16 GB (Low-RAM Protection Active)'
    elif total_ram_gb < 21.5:
        ram_tier = '16 GB Tier'
    elif total_ram_gb < 29.5:
        ram_tier = '24 GB Tier'
    else:
        ram_tier = '32 GB+ Workstation Tier'

    active_obj = os.path.abspath(getattr(vlm, 'model_path', '') or '')
    active_face = os.path.abspath(SFACE_MODEL_PATH)

    out_catalog = {}
    for cat, items in MODEL_CATALOG.items():
        enriched = []
        for item in items:
            p = item['path']
            exists = os.path.exists(p)
            size_mb = round(os.path.getsize(p) / (1024 * 1024), 2) if exists else None
            zone, zone_note = _evaluate_ram_zone(item['weight_tier'], total_ram_gb)
            is_active = (os.path.abspath(p) == active_obj) if cat == 'object_detection' else (os.path.abspath(p) == active_face)
            enriched.append({
                'id': item['id'],
                'name': item['name'],
                'path': p,
                'exists': exists,
                'size_mb': size_mb,
                'weight_tier': item['weight_tier'],
                'ram_est': item['ram_est'],
                'description': item['description'],
                'usage': item['usage'],
                'zone': zone,
                'zone_note': zone_note,
                'active': is_active,
            })
        out_catalog[cat] = enriched

    return jsonify({
        'ok': True,
        'total_ram_gb': total_ram_gb,
        'ram_tier_label': ram_tier,
        'accelerator': get_accelerator_summary(ort.get_available_providers()),
        'catalog': out_catalog,
    })

@app.route('/api/v1/models/select', methods=['POST'])
def api_models_select():
    global SFACE_MODEL_PATH
    data = request.get_json(silent=True) or {}
    cat = str(data.get('category') or '')
    mid = str(data.get('id') or '')
    items = MODEL_CATALOG.get(cat, [])
    target = next((x for x in items if x['id'] == mid), None)
    if not target:
        return jsonify({'ok': False, 'error': 'Unknown model id'}), 404
    if not os.path.exists(target['path']):
        return jsonify({'ok': False, 'error': f"Model file not found on disk: {target['path']}"}), 400
    try:
        if cat == 'object_detection':
            vlm.model_path = target['path']
            vlm.reload()
        elif cat == 'face_recognition':
            SFACE_MODEL_PATH = target['path']
            init_cv_models()
        return jsonify({'ok': True, 'category': cat, 'id': mid, 'name': target['name']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

def _format_wroom_event_reason(ev):
    ev_type = str(ev.get('type') or 'CUSTOM')
    src = str(ev.get('src') or 'WROOM')
    val = ev.get('val', 0)
    time_ms = int(ev.get('time_ms') or 0)
    extra = str(ev.get('extra') or '').strip()
    if ev_type == 'DOOR_OPEN':
        desc = f"Door opened (count: {val})"
    elif ev_type == 'DOOR_CLOSED':
        desc = f"Door closed (was open {val}s)"
    elif ev_type == 'DOOR_TIMEOUT':
        desc = f"Door open timeout (>{val}s)"
    elif ev_type == 'MOTION_START':
        desc = f"Motion detected (count: {val})"
    elif ev_type == 'MOTION_END':
        desc = "Motion ended"
    elif ev_type == 'BOOT':
        desc = "WROOM booted"
    elif ev_type == 'WIFI_CONNECTED':
        desc = "WROOM Wi-Fi connected"
    else:
        desc = f"{src} {ev_type} (val={val})"
    if extra:
        desc += f" [{extra}]"
    return f"{desc} (uptime {time_ms / 1000.0:.1f}s)"


def _wroom_event_poller_thread():
    """Poll ESP32-WROOM /events and persist new hardware/sensor logs into EventDB."""
    seen_keys = []
    max_time_ms = -1
    first_poll = True
    wroom_was_online = False
    wroom_fail_streak = 0
    recent_reasons = set()
    try:
        existing = event_db.query_events(limit=50, event_type='wroom').get('items', [])
        recent_reasons = {(r.get('person_label'), r.get('reason')) for r in existing}
    except Exception:
        pass

    while stream_active:
        try:
            req = urllib.request.Request(
                f"http://{WROOM_NODE_IP}:80/events",
                headers={'User-Agent': 'Sentinel-WROOM-Poller'},
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    wroom_was_online = True
                    wroom_fail_streak = 0
                    payload = json.loads(resp.read().decode('utf-8', errors='ignore'))
                    events = payload.get('events') or []
                    if events:
                        newest_ms = max(int(e.get('time_ms') or 0) for e in events)
                        if max_time_ms >= 0 and newest_ms + 5000 < max_time_ms:
                            # WROOM rebooted (millis reset)
                            seen_keys.clear()
                            max_time_ms = -1

                        for ev in reversed(events):
                            ev_type = str(ev.get('type') or 'CUSTOM')
                            src = str(ev.get('src') or 'WROOM')
                            time_ms = int(ev.get('time_ms') or 0)
                            key = (ev_type, src, time_ms)
                            if key in seen_keys:
                                continue
                            seen_keys.append(key)
                            if len(seen_keys) > 64:
                                seen_keys.pop(0)
                            if time_ms > max_time_ms:
                                max_time_ms = time_ms

                            label = f"{src}: {ev_type}"
                            reason = _format_wroom_event_reason(ev)
                            if first_poll and (label, reason) in recent_reasons:
                                continue

                            if not first_poll and ev_type == 'DOOR_OPEN':
                                global door_open_seq
                                with door_recording_lock:
                                    door_open_seq += 1
                            event_db.log_recognition_event(
                                event_type='wroom',
                                person_id='ESP32-WROOM',
                                person_label=label,
                                similarity=1.0,
                                quality=100.0,
                                reason=reason,
                            )
                        first_poll = False
        except Exception:
            if wroom_was_online:
                wroom_fail_streak += 1
                if wroom_fail_streak >= 16:
                    # WROOM went offline unexpectedly (~8s): record 10s safety clip once
                    wroom_was_online = False
                    wroom_fail_streak = 0
                    print("[SAFETY] ESP32-WROOM went offline unexpectedly -> recording 10s safety clip")
                    notification_manager.emit(NotificationEvent(
                        event_type='WROOM_OFFLINE',
                        title="WROOM Offline / Tamper Alert",
                        message="ESP32-WROOM went offline unexpectedly — saving 10s safety recording.",
                        severity='critical',
                        dedup_key="WROOM_OFFLINE",
                    ))
                    cam_ip = active_camera_ip or (CANDIDATE_IPS[0] if CANDIDATE_IPS else '192.168.29.10')
                    try:
                        urllib.request.urlopen(f"http://{cam_ip}:80/api/v1/camera/power?state=on", timeout=2.5).read()
                    except Exception:
                        pass
                    start_door_recording(duration_s=10.0, bump_seq=False)
                    def _restore_standby(ip=cam_ip):
                        try:
                            urllib.request.urlopen(f"http://{ip}:80/api/v1/camera/power?state=off", timeout=2.5).read()
                        except Exception:
                            pass
                    threading.Timer(11.0, _restore_standby).start()
        time.sleep(0.5)


_mdns_zc = None
_mdns_info = None


def _get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.29.1", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def _start_mdns_advertiser(port, mdns_host="doorcam"):
    global _mdns_zc, _mdns_info
    try:
        from zeroconf import Zeroconf, ServiceInfo
        lan_ip = _get_lan_ip()
        clean_host = mdns_host.strip().lower().removesuffix('.local') or 'doorcam'
        _mdns_info = ServiceInfo(
            "_http._tcp.local.",
            f"{clean_host}._http._tcp.local.",
            addresses=[socket.inet_aton(lan_ip)],
            port=int(port),
            properties={"path": "/"},
            server=f"{clean_host}.local.",
        )
        _mdns_zc = Zeroconf()
        _mdns_zc.register_service(_mdns_info)
        return f"http://{clean_host}.local:{port}", lan_ip
    except Exception as e:
        print(f"[!] mDNS advertiser unavailable: {e}")
        return None, _get_lan_ip()


def _stop_mdns_advertiser():
    global _mdns_zc, _mdns_info
    try:
        if _mdns_zc and _mdns_info:
            _mdns_zc.unregister_service(_mdns_info)
        if _mdns_zc:
            _mdns_zc.close()
    except Exception:
        pass
    _mdns_zc = None
    _mdns_info = None


def _graceful_shutdown():
    global stream_active
    stream_active = False
    _stop_mdns_advertiser()
    try:
        notification_manager._running = False
    except Exception:
        pass
    try:
        with state_lock:
            save_encodings()
            save_profiles()
            save_detections()
            save_labels()
    except Exception:
        pass

atexit.register(_graceful_shutdown)

# ============================================================================
# INITIALIZATION & ENTRYPOINT
# ============================================================================
init_cv_models()
known_face_encodings = load_encodings()
identity_profiles = load_profiles()
person_labels = load_labels()
person_detections = load_detections()
sync_and_backfill_profiles()
migrated_events = event_db.backfill_from_detections(person_detections, person_labels)
if migrated_events:
    print(f"[*] Phase 9 EventDB: migrated {migrated_events} historical events into data/events.db")

backup_daemon = threading.Thread(target=backup_thread, daemon=True)
backup_daemon.start()

stream_active = True
mjpeg_thread = threading.Thread(target=mjpeg_consumer_thread, daemon=True)
mjpeg_thread.start()

ai_thread = threading.Thread(target=ai_worker_thread, daemon=True)
ai_thread.start()

def _door_udp_listener_thread():
    """Listen on UDP port 5055 for sub-millisecond DOOR_OPEN/DOOR_CLOSED triggers from WROOM."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('0.0.0.0', 5055))
        sock.settimeout(1.0)
    except Exception as e:
        print(f"[UDP] Door listener bind failed on :5055: {e}")
        return

    while stream_active:
        try:
            data, _addr = sock.recvfrom(512)
            msg = data.decode('utf-8', errors='ignore').upper()
            if 'DOOR_OPEN' in msg or 'MOTION_START' in msg:
                start_door_recording(duration_s=20.0)
            elif 'DOOR_CLOSED' in msg:
                stop_door_recording_tail(tail_s=5.0)
        except socket.timeout:
            continue
        except Exception:
            time.sleep(0.1)

wroom_poller_thread = threading.Thread(target=_wroom_event_poller_thread, daemon=True)
wroom_poller_thread.start()

door_udp_thread = threading.Thread(target=_door_udp_listener_thread, daemon=True)
door_udp_thread.start()

if __name__ == '__main__':
    print("=" * 65)
    print("[*] ESP32-CAM Smart Door Entry & Facial Identity Server")
    print("=" * 65)
    print(f"Candidates: {get_candidate_stream_urls()}")
    print(f"Data folder: {os.path.abspath(DATA_FOLDER)}")
    print(f"Recognition Pipeline: {MODEL_NAME}")
    print(f"Thresholds: Strong={SFACE_MATCH_STRONG} | Standard={SFACE_MATCH_STANDARD} | Inconclusive>={SFACE_INCONCLUSIVE_LOW}")
    print(f"Enrolled profiles: {len(identity_profiles)}")
    print("=" * 65)
    PORT = int(os.environ.get('PORT', 5000))
    MDNS_HOST = os.environ.get('MDNS_HOST', 'doorcam')
    mdns_url, lan_ip = _start_mdns_advertiser(PORT, MDNS_HOST)
    print(f"Web dashboard: http://localhost:{PORT}")
    print(f"LAN IP URL:    http://{lan_ip}:{PORT}")
    if mdns_url:
        print(f"LAN DNS URL:   {mdns_url}")
    print("=" * 65)

    try:
        from waitress import serve
        print(f"WSGI Server:   Waitress production server (threads=16)")
        print("=" * 65)
        serve(app, host='0.0.0.0', port=PORT, threads=16, channel_timeout=120)
    except ImportError:
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    finally:
        stream_active = False
