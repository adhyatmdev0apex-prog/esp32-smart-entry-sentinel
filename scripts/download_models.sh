#!/usr/bin/env bash
# ============================================================================
# download_models.sh
#
# Fetches the AI model weights that server.py and vlm_engine.py expect at
# runtime, so they don't have to live in Git.
#
# Covers:
#   models/face_detection_yunet_2023mar.onnx   (YuNet face detector)
#   models/insightface/recognition/model.onnx  (InsightFace MobileFaceNet)
#   models/yolov8n.onnx                        (YOLOv8n object detector)
#
# NOT downloaded (and not needed): models/insightface/detection/model.onnx
#   — this file existed in the old repo but nothing in server.py or
#   vlm_engine.py ever reads it (face detection uses YuNet, not InsightFace's
#   own detector). Confirmed by grep across both files before writing this
#   script. If you're relying on it somewhere outside this repo, add your
#   own fetch step.
#
# NOTE on yolov8n.onnx: server.py has NO auto-download for this one (only
# YuNet + InsightFace have that built in, via ensure_models_downloaded() in
# server.py itself — this script's first two steps just mirror that same
# logic so you can pre-fetch everything in one go before first run).
# vlm_engine.py hardcodes input_size = 320 and resizes frames to 320x320
# itself, so a model exported with dynamic/any input size works fine here.
# This script exports a fresh yolov8n.onnx at imgsz=320 via Ultralytics,
# which should be functionally equivalent to whatever was in the original
# repo — but if the original file was a custom export with extra
# quantization/pruning, this will NOT be byte-identical. If you have the
# original yolov8n.onnx backed up anywhere, prefer that instead.
# ============================================================================

set -e

MODELS_DIR="models"
mkdir -p "$MODELS_DIR/insightface/recognition"

echo "=== [1/3] YuNet face detector ==="
YUNET_PATH="$MODELS_DIR/face_detection_yunet_2023mar.onnx"
if [ -f "$YUNET_PATH" ]; then
  echo "Already present, skipping: $YUNET_PATH"
else
  YUNET_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
  echo "Downloading from $YUNET_URL"
  curl -fL -o "$YUNET_PATH" "$YUNET_URL" || wget -O "$YUNET_PATH" "$YUNET_URL"
fi

echo
echo "=== [2/3] InsightFace MobileFaceNet (512-D ArcFace recognition) ==="
SFACE_PATH="$MODELS_DIR/insightface/recognition/model.onnx"
if [ -f "$SFACE_PATH" ]; then
  echo "Already present, skipping: $SFACE_PATH"
else
  python3 -c "
from huggingface_hub import hf_hub_download
import shutil, os
path = hf_hub_download(repo_id='immich-app/buffalo_s', filename='recognition/model.onnx', local_dir='$MODELS_DIR/insightface')
print('Downloaded to', path)
" || {
    echo "huggingface_hub not installed — installing it now..."
    pip install --break-system-packages -q huggingface_hub
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(repo_id='immich-app/buffalo_s', filename='recognition/model.onnx', local_dir='$MODELS_DIR/insightface')
"
  }
fi

echo
echo "=== [3/3] YOLOv8n object detector (320x320 export) ==="
YOLO_PATH="$MODELS_DIR/yolov8n.onnx"
if [ -f "$YOLO_PATH" ]; then
  echo "Already present, skipping: $YOLO_PATH"
else
  echo "This step needs the 'ultralytics' package (pulls in PyTorch, ~a few hundred MB)."
  python3 -c "
from ultralytics import YOLO
m = YOLO('yolov8n.pt')
m.export(format='onnx', imgsz=320)
import shutil, os
shutil.move('yolov8n.onnx', '$YOLO_PATH')
print('Exported to $YOLO_PATH')
" || {
    echo "ultralytics not installed — installing it now..."
    pip install --break-system-packages -q ultralytics
    python3 -c "
from ultralytics import YOLO
m = YOLO('yolov8n.pt')
m.export(format='onnx', imgsz=320)
import shutil
shutil.move('yolov8n.onnx', '$YOLO_PATH')
"
  }
fi

echo
echo "=== Done. Model files present: ==="
find "$MODELS_DIR" -name "*.onnx" -exec ls -lh {} \;
