#!/usr/bin/env python3
"""
AI CCTV - 라즈베리파이5 송신부
- 포트 9999: 감지 이벤트 (이미지 저장 + CSV)
- 포트 9998: 실시간 스트리밍 (라이브 뷰)
- 포트 9997: ROI 좌표 수신 (Windows → 라즈베리파이)
"""

import socket
import struct
import json
import time
import threading
import cv2
import numpy as np
from datetime import datetime
from picamera2 import Picamera2
from ultralytics import YOLO

# ──────────────────────────────────────────────
# 설정값
# ──────────────────────────────────────────────
WINDOWS_HOST         = "192.168.0.6"
WINDOWS_PORT         = 9999
STREAM_PORT          = 9998
ROI_PORT             = 9997   # ROI 수신 포트
COOLDOWN_SEC         = 3.0
CONFIDENCE_THRESHOLD = 0.50
MODEL_PATH           = "yolov8n.pt"
FRAME_WIDTH          = 640
FRAME_HEIGHT         = 480
STREAM_FPS           = 15
# ──────────────────────────────────────────────

PERSON_CLASS_ID = 0

# 전역 ROI (None이면 전체 화면)
current_roi = {"rect": None}  # {"rect": (x1, y1, x2, y2)} or None
roi_lock = threading.Lock()


# ── ROI 수신 서버 ──
class ROIReceiver(threading.Thread):
    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", self.port))
        server.listen(1)
        print(f"[*] ROI 수신 서버 → 포트 {self.port}")

        while True:
            try:
                conn, addr = server.accept()
                data = b""
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                conn.close()

                roi_data = json.loads(data.decode("utf-8"))
                with roi_lock:
                    pts = roi_data.get("roi")
                    if pts is None:
                        current_roi["rect"] = None
                        print("[ROI] 초기화 → 전체 화면 감지")
                    else:
                        # pts = [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
                        x1, y1 = pts[0]
                        x2, y2 = pts[2]
                        current_roi["rect"] = (x1, y1, x2, y2)
                        print(f"[ROI] 설정: ({x1},{y1}) → ({x2},{y2})")
            except Exception as e:
                print(f"[!] ROI 수신 오류: {e}")


def send_event(sock, timestamp, confidence, count, image_bytes):
    header = json.dumps({
        "timestamp":  timestamp,
        "confidence": round(float(confidence), 4),
        "count":      count,
    }).encode("utf-8")
    packet = (
        struct.pack(">I", len(header)) + header +
        struct.pack(">I", len(image_bytes)) + image_bytes
    )
    sock.sendall(packet)


def connect_to_server(host, port, retry=5.0):
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((host, port))
            s.settimeout(None)
            print(f"[✓] 연결 성공: {host}:{port}")
            return s
        except (ConnectionRefusedError, OSError):
            print(f"[!] 연결 실패 ({host}:{port}) → {retry}초 후 재시도...")
            time.sleep(retry)


class StreamSender(threading.Thread):
    def __init__(self, host, port, frame_getter):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.frame_getter = frame_getter
        self._sock = None

    def run(self):
        interval = 1.0 / STREAM_FPS
        while True:
            try:
                self._sock = connect_to_server(self.host, self.port)
                while True:
                    frame = self.frame_getter()
                    if frame is None:
                        time.sleep(0.01)
                        continue
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    data = buf.tobytes()
                    self._sock.sendall(struct.pack(">I", len(data)) + data)
                    time.sleep(interval)
            except (BrokenPipeError, OSError):
                print("[!] 스트리밍 연결 끊김 → 재연결...")
                if self._sock:
                    self._sock.close()


def apply_roi_mask(frame, roi):
    """ROI 영역 외부를 어둡게 처리 + ROI 박스 표시"""
    if roi is None:
        return frame
    x1, y1, x2, y2 = roi
    overlay = frame.copy()
    # 전체를 반투명 어둡게
    mask = np.zeros_like(frame)
    cv2.rectangle(mask, (0, 0), (frame.shape[1], frame.shape[0]), (0, 0, 0), -1)
    result = cv2.addWeighted(frame, 0.4, mask, 0.6, 0)
    # ROI 영역은 원본
    result[y1:y2, x1:x2] = frame[y1:y2, x1:x2]
    # ROI 테두리
    cv2.rectangle(result, (x1, y1), (x2, y2), (0, 255, 255), 2)
    cv2.putText(result, "ROI", (x1, max(y1 - 6, 0)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    return result


def main():
    print("[*] AI CCTV 라즈베리파이 송신부 시작")

    model = YOLO(MODEL_PATH)
    print("[✓] YOLOv8 모델 로드 완료")

    picam2 = Picamera2()
    cam_config = picam2.create_preview_configuration(
        main={"format": "RGB888", "size": (FRAME_WIDTH, FRAME_HEIGHT)}
    )
    picam2.configure(cam_config)
    picam2.start()
    time.sleep(1.0)
    print("[✓] 카메라 초기화 완료")

    latest_frame = {"data": None}

    # ROI 수신 서버 시작
    ROIReceiver(ROI_PORT).start()

    # 스트리밍 스레드 시작
    StreamSender(WINDOWS_HOST, STREAM_PORT, lambda: latest_frame["data"]).start()

    sock = connect_to_server(WINDOWS_HOST, WINDOWS_PORT)
    last_sent = 0.0

    try:
        while True:
            frame = picam2.capture_array()

            with roi_lock:
                roi = current_roi["rect"]

            # ROI 적용한 스트리밍 프레임 (시각화용)
            stream_frame = apply_roi_mask(frame.copy(), roi)
            latest_frame["data"] = stream_frame

            # YOLO 추론: ROI가 있으면 해당 영역만 크롭해서 추론
            if roi:
                x1, y1, x2, y2 = roi
                crop = frame[y1:y2, x1:x2]
                infer_frame = crop
            else:
                infer_frame = frame

            results = model.predict(
                source=infer_frame,
                classes=[PERSON_CLASS_ID],
                conf=CONFIDENCE_THRESHOLD,
                verbose=False,
            )

            boxes = results[0].boxes
            person_count = len(boxes)
            now = time.time()

            if person_count > 0 and (now - last_sent) >= COOLDOWN_SEC:
                best_conf = float(boxes.conf.max().item())

                # 박스 그리기 (원본 프레임에 ROI 오프셋 적용)
                annotated = frame.copy()
                ox, oy = (roi[0], roi[1]) if roi else (0, 0)
                for box in boxes:
                    bx1, by1, bx2, by2 = map(int, box.xyxy[0])
                    bx1 += ox; by1 += oy; bx2 += ox; by2 += oy
                    conf_val = float(box.conf[0])
                    cv2.rectangle(annotated, (bx1, by1), (bx2, by2), (0, 255, 80), 2)
                    cv2.putText(annotated, f"Person {conf_val:.2f}",
                                (bx1, max(by1 - 8, 0)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 80), 2)
                cv2.putText(annotated, f"Count: {person_count}",
                            (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255), 2)

                # ROI 테두리도 표시
                if roi:
                    cv2.rectangle(annotated, (roi[0], roi[1]), (roi[2], roi[3]), (0, 255, 255), 2)

                _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
                image_bytes = buf.tobytes()
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                try:
                    send_event(sock, timestamp, best_conf, person_count, image_bytes)
                    print(f"[→] 전송 | {timestamp} | 감지: {person_count}명 | 신뢰도: {best_conf:.2f}")
                    last_sent = now
                except (BrokenPipeError, OSError):
                    print("[!] 이벤트 연결 끊김 → 재연결...")
                    sock.close()
                    sock = connect_to_server(WINDOWS_HOST, WINDOWS_PORT)

    except KeyboardInterrupt:
        print("\n[*] 종료")
    finally:
        picam2.stop()
        sock.close()


if __name__ == "__main__":
    main()