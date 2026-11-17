"""
DirectML GPU Object Detection Engine (YOLOv8n ONNX)
===================================================
Lightweight real-time object detector designed for thin-and-light laptops:
- Target Hardware: AMD Ryzen 5 AI + Radeon 840M iGPU
- CPU Budget: <= 40% total CPU (offloaded to DirectML GPU + adaptive rate limit)
- Model: YOLOv8n ONNX (320x320, ~12.1 MB, 80 COCO object classes)
- Outputs bounding boxes + class names to overlay alongside face recognition.
"""

import os
import time
import threading
import queue
import cv2
import numpy as np

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import onnxruntime as ort
    HAS_ORT = True
    AVAILABLE_PROVIDERS = ort.get_available_providers()
except ImportError:
    HAS_ORT = False
    AVAILABLE_PROVIDERS = []

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush"
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
YOLO11S_MODEL = os.path.join(BASE_DIR, "models", "yolo11s.onnx")
DEFAULT_YOLO_MODEL = YOLO11S_MODEL if os.path.exists(YOLO11S_MODEL) else os.path.join(BASE_DIR, "models", "yolov8n.onnx")


class ObjectDetectionEngine:
    def __init__(self, model_path=DEFAULT_YOLO_MODEL, use_gpu=True, cooldown_s=0.22):
        is_y11s = os.path.basename(model_path).startswith("yolo11s")
        self.model_name = "yolo11s-416 (COCO 80 classes)" if is_y11s else "yolov8n-320 (COCO 80 classes)"
        self.model_path = model_path
        self.use_gpu = use_gpu
        self.cooldown_s = cooldown_s
        self.min_cooldown_s = 0.14
        self.max_cooldown_s = 0.65
        self.input_size = 416 if is_y11s else 320
        self.last_inference_time = 0.0
        self.last_infer_ms = 0.0
        self.gpu_duty_pct = 0.0
        self.last_cpu_pct = 0.0
        self.lock = threading.Lock()
        self.is_loaded = False
        self.session = None
        self.input_name = None
        self.active_provider = "CPU (Fallback)"
        self.enabled = True

        self.latest_objects = []
        self.latest_boxes = []
        self.latest_caption = "Scanning for objects..."
        self.caption_history = []
        self.task_queue = queue.Queue(maxsize=4)

        from npu import get_execution_providers
        self.providers, self.active_provider = get_execution_providers(
            AVAILABLE_PROVIDERS, use_gpu=self.use_gpu, use_npu=True
        )
        self.device_label = self.active_provider

        self._load_model()

        if HAS_PSUTIL:
            psutil.cpu_percent(interval=None)

        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()

    def _load_model(self):
        if not HAS_ORT or not os.path.exists(self.model_path):
            print(f"[!] ObjectDetector: model missing at {self.model_path}")
            return
        try:
            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = 2
            sess_opts.inter_op_num_threads = 1
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # Disable spinning to keep idle/inference CPU below 40%
            sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self.session = ort.InferenceSession(
                self.model_path, sess_options=sess_opts, providers=self.providers
            )
            self.input_name = self.session.get_inputs()[0].name
            actual = self.session.get_providers()
            if "DmlExecutionProvider" in actual:
                self.active_provider = "DirectML (AMD Radeon 840M GPU)"
            elif "CUDAExecutionProvider" in actual:
                self.active_provider = "CUDA GPU"
            else:
                self.active_provider = "CPU (Fallback)"
            self.is_loaded = True
            print(f"[*] ObjectDetector loaded {self.model_path} on {actual}")
        except Exception as e:
            print(f"[!] ObjectDetector failed to load: {e}")
            self.is_loaded = False

    def reload(self):
        """Reload the DirectML ONNX session and reset cached detections."""
        with self.lock:
            self.session = None
            self.is_loaded = False
            self.latest_objects = []
            self.latest_boxes = []
            self.latest_caption = "Reloading object detector..."
            self.last_inference_time = 0.0
            self._load_model()
            self.latest_caption = "Scanning for objects..." if self.is_loaded else "Model reload failed"
        return self.is_loaded

    def get_status(self):
        with self.lock:
            objs = list(self.latest_objects)
            caption = self.latest_caption
            hist = list(self.caption_history)[-8:]
        return {
            "model": self.model_name,
            "enabled": getattr(self, "enabled", True),
            "max_weight_size": "36.2 MB" if "yolo11s" in self.model_name else "12.1 MB",
            "active_provider": self.active_provider,
            "gpu_accelerated": ("DirectML" in self.active_provider or "CUDA" in self.active_provider),
            "is_loaded": self.is_loaded,
            "latest_caption": caption,
            "detected_objects": objs,
            "caption_history": hist,
            "cooldown_s": round(self.cooldown_s, 3),
            "gpu_duty_pct": round(self.gpu_duty_pct, 1),
            "cpu_pct": round(self.last_cpu_pct, 1),
            "resource_cap": "<= 36% CPU | 7-9% GPU (cap 15%)"
        }

    def _letterbox(self, bgr_img):
        h, w = bgr_img.shape[:2]
        size = self.input_size
        scale = min(size / float(w), size / float(h))
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(bgr_img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        pad_x = (size - nw) // 2
        pad_y = (size - nh) // 2
        canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(rgb, (2, 0, 1))[None, ...]
        return blob, scale, pad_x, pad_y, w, h

    def detect_objects(self, img_input, conf_thresh=0.42, iou_thresh=0.45, force=False):
        """
        Runs YOLOv8n ONNX object detection on DirectML GPU.
        Returns cached boxes between cooldown ticks or if CPU > 36% to keep CPU <= 40%.
        Each box dict: {'bbox': (top, right, bottom, left), 'label': str, 'conf': float, 'class_id': int}
        """
        if not getattr(self, "enabled", True) or not self.is_loaded:
            return []

        now = time.time()
        if not force and (now - self.last_inference_time) < self.cooldown_s:
            with self.lock:
                return list(self.latest_boxes)

        if not force and HAS_PSUTIL:
            cpu_now = psutil.cpu_percent(interval=None)
            self.last_cpu_pct = cpu_now
            if cpu_now > 36.0:
                self.cooldown_s = min(self.max_cooldown_s, self.cooldown_s + 0.05)
                with self.lock:
                    return list(self.latest_boxes)

        if isinstance(img_input, bytes):
            nparr = np.frombuffer(img_input, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(img_input, np.ndarray):
            img = img_input
        else:
            return []

        if img is None or img.size == 0:
            return []

        with self.lock:
            prev_t = self.last_inference_time
            t0 = time.perf_counter()
            self.last_inference_time = time.time()
            blob, scale, pad_x, pad_y, orig_w, orig_h = self._letterbox(img)
            try:
                outputs = self.session.run(None, {self.input_name: blob})[0]
            except Exception as infer_err:
                if self.providers != ["CPUExecutionProvider"]:
                    print(f"[!] ObjectDetector EP error ({infer_err}), falling back to CPUExecutionProvider")
                    self.providers = ["CPUExecutionProvider"]
                    self.active_provider = "CPU (Fallback)"
                    self._load_model()
                    outputs = self.session.run(None, {self.input_name: blob})[0]
                else:
                    raise
            infer_ms = (time.perf_counter() - t0) * 1000.0
            self.last_infer_ms = infer_ms
            if prev_t > 0:
                dt_ms = max(infer_ms, (self.last_inference_time - prev_t) * 1000.0)
                duty = min(100.0, (infer_ms / dt_ms) * 100.0)
                self.gpu_duty_pct = 0.7 * self.gpu_duty_pct + 0.3 * duty if self.gpu_duty_pct > 0 else duty
                # Adaptive governor: target 7-9% GPU (hard cap 15%), CPU <= 36%
                if self.gpu_duty_pct > 14.5 or self.last_cpu_pct > 32.0:
                    self.cooldown_s = min(self.max_cooldown_s, self.cooldown_s + 0.04)
                elif self.gpu_duty_pct < 7.0 and self.last_cpu_pct < 28.0:
                    self.cooldown_s = max(self.min_cooldown_s, self.cooldown_s - 0.02)

            preds = np.squeeze(outputs, axis=0).T  # (2100, 84)
            boxes_xywh = preds[:, :4]
            class_scores = preds[:, 4:]
            class_ids = np.argmax(class_scores, axis=1)
            confidences = class_scores[np.arange(len(preds)), class_ids]

            mask = confidences >= conf_thresh
            boxes_xywh = boxes_xywh[mask]
            confidences = confidences[mask]
            class_ids = class_ids[mask]

            results = []
            if len(boxes_xywh) > 0:
                nms_boxes = []
                for cx, cy, bw, bh in boxes_xywh:
                    x1 = (cx - bw * 0.5 - pad_x) / scale
                    y1 = (cy - bh * 0.5 - pad_y) / scale
                    w_box = bw / scale
                    h_box = bh / scale
                    nms_boxes.append([int(round(x1)), int(round(y1)), int(round(w_box)), int(round(h_box))])

                indices = cv2.dnn.NMSBoxes(nms_boxes, confidences.tolist(), conf_thresh, iou_thresh)
                if len(indices) > 0:
                    for idx in np.array(indices).flatten():
                        x1, y1, wb, hb = nms_boxes[idx]
                        left = max(0, min(orig_w - 1, x1))
                        top = max(0, min(orig_h - 1, y1))
                        right = max(0, min(orig_w - 1, x1 + wb))
                        bottom = max(0, min(orig_h - 1, y1 + hb))
                        if right - left < 8 or bottom - top < 8:
                            continue
                        cid = int(class_ids[idx])
                        cname = COCO_CLASSES[cid] if 0 <= cid < len(COCO_CLASSES) else f"obj_{cid}"
                        conf = float(confidences[idx])
                        results.append({
                            "bbox": (top, right, bottom, left),
                            "label": cname,
                            "conf": round(conf, 3),
                            "class_id": cid
                        })

            self.latest_boxes = results
            self.latest_objects = [
                {"name": r["label"], "confidence": round(r["conf"] * 100, 1)}
                for r in results
            ]
            if results:
                summary = ", ".join(f"{r['label']} ({int(r['conf'] * 100)}%)" for r in results[:6])
                self.latest_caption = f"Objects: {summary}"
                self._record_history(self.latest_caption)
            else:
                self.latest_caption = "No objects detected in view"
            return list(results)

    def describe_image(self, img_input, prompt=None):
        """Compatibility wrapper: runs object detection and returns detected object names."""
        if not getattr(self, "enabled", True):
            return "Object Detection Engine is paused."
        self.detect_objects(img_input, force=True)
        return self.latest_caption

    def queue_async_description(self, img_bytes, callback=None):
        if not getattr(self, "enabled", True):
            return
        try:
            self.task_queue.put_nowait((img_bytes, callback))
        except queue.Full:
            pass

    def _process_queue(self):
        while True:
            try:
                img_bytes, callback = self.task_queue.get(timeout=1.0)
                caption = self.describe_image(img_bytes)
                if callback:
                    try:
                        callback(caption)
                    except Exception:
                        pass
                self.task_queue.task_done()
                time.sleep(0.5)
            except queue.Empty:
                continue

    def _record_history(self, caption):
        timestamp = time.strftime("%H:%M:%S")
        if self.caption_history and self.caption_history[-1]["caption"] == caption:
            return
        self.caption_history.append({"time": timestamp, "caption": caption})
        if len(self.caption_history) > 20:
            self.caption_history.pop(0)


# Global singleton instance (alias preserved as `vlm` for server.py API compatibility)
vlm = ObjectDetectionEngine()
