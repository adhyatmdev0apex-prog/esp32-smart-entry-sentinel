"""
Phase 10.4: MQTT Provider
Publishes to topics under base_topic (default: sentinel):
  sentinel/events/door
  sentinel/events/person
  sentinel/events/security
  sentinel/status
Uses paho-mqtt if installed, otherwise falls back to a minimal MQTT 3.1.1 TCP client.
"""

import json
import socket
import struct


def _topic_for_event(base_topic, event_type):
    base = (base_topic or "sentinel").strip("/")
    if event_type in ("DOOR_OPEN", "MOTION_DETECTED"):
        return f"{base}/events/door"
    if event_type in ("KNOWN_PERSON", "UNKNOWN_PERSON"):
        return f"{base}/events/person"
    if event_type in ("CAMERA_OFFLINE", "SYSTEM_FAILURE"):
        return f"{base}/status"
    return f"{base}/events/security"


def _encode_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _encode_remaining_len(x):
    out = bytearray()
    while True:
        byte = x % 128
        x //= 128
        if x > 0:
            byte |= 0x80
        out.append(byte)
        if x == 0:
            break
    return bytes(out)


def _minimal_mqtt_publish(host, port, topic, payload_bytes, username="", password="", timeout_s=3.0):
    sock = socket.create_connection((host, int(port)), timeout=timeout_s)
    try:
        sock.settimeout(timeout_s)
        flags = 0x02  # Clean session
        vh = _encode_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 30)
        pl = _encode_str("sentinel-backend")
        if username:
            flags |= 0x80
            pl += _encode_str(username)
            if password:
                flags |= 0x40
                pl += _encode_str(password)
            vh = _encode_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 30)

        connect_pkt = bytes([0x10]) + _encode_remaining_len(len(vh) + len(pl)) + vh + pl
        sock.sendall(connect_pkt)
        connack = sock.recv(4)
        if len(connack) < 4 or connack[0] != 0x20 or connack[3] != 0x00:
            return False, f"CONNACK rejected ({connack!r})"

        pub_vh = _encode_str(topic)
        pub_pkt = bytes([0x30]) + _encode_remaining_len(len(pub_vh) + len(payload_bytes)) + pub_vh + payload_bytes
        sock.sendall(pub_pkt)
        sock.sendall(bytes([0xE0, 0x00]))  # DISCONNECT
        return True, f"Published to {topic}"
    finally:
        sock.close()


def send(event, cfg):
    host = (cfg.get("host") or "").strip()
    if not host:
        return False, "MQTT host not configured"
    port = int(cfg.get("port", 1883))
    timeout_s = float(cfg.get("timeout_s", 3.0))
    topic = _topic_for_event(cfg.get("base_topic", "sentinel"), event.event_type)
    payload = json.dumps({
        "event_id": event.event_id,
        "event_type": event.event_type,
        "severity": event.severity,
        "title": event.title,
        "message": event.message,
        "person_id": event.person_id,
        "person_label": event.person_label,
        "timestamp": event.timestamp,
        "metadata": event.metadata,
    }).encode("utf-8")

    username = cfg.get("username") or ""
    password = cfg.get("password") or ""

    try:
        import paho.mqtt.publish as publish
        auth = {"username": username, "password": password} if username else None
        publish.single(topic, payload=payload, hostname=host, port=port, auth=auth, keepalive=15)
        return True, f"Published to {topic} (paho)"
    except ImportError:
        return _minimal_mqtt_publish(host, port, topic, payload, username, password, timeout_s)
