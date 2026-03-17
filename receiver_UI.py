#!/usr/bin/env python3
"""
AI CCTV - Windows 수신부 (PyQt5 대시보드)
- 포트 9999: 감지 이벤트 수신 → 이미지 저장 + CSV
- 포트 9998: 실시간 스트리밍 라이브 뷰
- 포트 9997: ROI 좌표 전송 → 라즈베리파이
"""

import sys
import os
import csv
import socket
import struct
import json
import threading
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QProgressBar, QPushButton,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QRect, QPoint
from PyQt5.QtGui import QPixmap, QImage, QColor, QPainter, QPen, QBrush

# ──────────────────────────────────────────────
# 설정값
# ──────────────────────────────────────────────
RPI_HOST     = "192.168.0.48"  # 라즈베리파이 IP
SERVER_HOST  = "0.0.0.0"
EVENT_PORT   = 9999
STREAM_PORT  = 9998
ROI_PORT     = 9997
SAVE_DIR     = "captures"
CSV_FILE     = "detections.csv"
MAX_LOG_ROWS = 500
# ──────────────────────────────────────────────


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("연결 종료")
        buf += chunk
    return buf


def ensure_csv(path):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp", "image_filename", "confidence"])


def append_csv(path, timestamp, filename, confidence):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([timestamp, filename, f"{confidence:.4f}"])


def send_roi_to_rpi(roi_points):
    """ROI 좌표를 라즈베리파이로 전송"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((RPI_HOST, ROI_PORT))
        data = json.dumps({"roi": roi_points}).encode("utf-8")
        s.sendall(data)
        s.close()
        print(f"[→] ROI 전송: {roi_points}")
    except Exception as e:
        print(f"[!] ROI 전송 실패: {e}")


# ── 감지 이벤트 수신 스레드 ──
class EventServerThread(QThread):
    event_received = pyqtSignal(str, float, int, bytes, str)

    def run(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        ensure_csv(CSV_FILE)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((SERVER_HOST, EVENT_PORT))
        server.listen(5)
        print(f"[*] 이벤트 서버 → 포트 {EVENT_PORT}")

        while True:
            try:
                conn, addr = server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except Exception as e:
                print(f"[!] 서버 오류: {e}")

    def _handle(self, conn):
        try:
            while True:
                header_len  = struct.unpack(">I", recv_exact(conn, 4))[0]
                header      = json.loads(recv_exact(conn, header_len).decode("utf-8"))
                image_len   = struct.unpack(">I", recv_exact(conn, 4))[0]
                image_bytes = recv_exact(conn, image_len)

                ts         = header["timestamp"]
                confidence = header["confidence"]
                count      = header.get("count", 1)

                safe_ts  = ts.replace(":", "").replace(" ", "_").replace("-", "")
                filename = f"capture_{safe_ts}.jpg"
                filepath = os.path.join(SAVE_DIR, filename)

                with open(filepath, "wb") as f:
                    f.write(image_bytes)

                append_csv(CSV_FILE, ts, filename, confidence)
                self.event_received.emit(ts, confidence, count, image_bytes, filename)
        except (ConnectionError, struct.error):
            pass
        finally:
            conn.close()


# ── 스트리밍 수신 스레드 ──
class StreamReceiverThread(QThread):
    frame_received = pyqtSignal(bytes)

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((SERVER_HOST, STREAM_PORT))
        server.listen(1)
        print(f"[*] 스트리밍 서버 → 포트 {STREAM_PORT}")

        while True:
            try:
                conn, _ = server.accept()
                self._handle(conn)
            except Exception:
                pass

    def _handle(self, conn):
        try:
            while True:
                size = struct.unpack(">I", recv_exact(conn, 4))[0]
                data = recv_exact(conn, size)
                self.frame_received.emit(data)
        except (ConnectionError, struct.error):
            pass
        finally:
            conn.close()


# ── ROI 드래그 가능한 라이브 뷰 라벨 ──
class LiveViewLabel(QLabel):
    roi_set = pyqtSignal(list)   # ROI 좌표 확정 시그널
    roi_cleared = pyqtSignal()   # ROI 초기화 시그널

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._drawing = False
        self._start   = QPoint()
        self._end     = QPoint()
        self._roi_rect = None      # 확정된 ROI QRect
        self._roi_mode = False     # ROI 설정 모드 여부
        self._pixmap_size = (632, 474)

    def set_roi_mode(self, enabled: bool):
        self._roi_mode = enabled
        if enabled:
            self.setCursor(Qt.CrossCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if self._roi_mode and e.button() == Qt.LeftButton:
            self._drawing = True
            self._start   = e.pos()
            self._end     = e.pos()

    def mouseMoveEvent(self, e):
        if self._drawing:
            self._end = e.pos()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drawing and e.button() == Qt.LeftButton:
            self._drawing  = False
            self._end      = e.pos()
            self._roi_rect = QRect(self._start, self._end).normalized()
            self.update()

            # 실제 프레임 좌표로 변환 (640x480 기준)
            pw, ph = self._pixmap_size
            sx = 640 / pw
            sy = 480 / ph
            x1 = int(self._roi_rect.left()   * sx)
            y1 = int(self._roi_rect.top()    * sy)
            x2 = int(self._roi_rect.right()  * sx)
            y2 = int(self._roi_rect.bottom() * sy)
            # 폴리곤 형식으로 전달 (4점)
            self.roi_set.emit([[x1,y1],[x2,y1],[x2,y2],[x1,y2]])
            self.set_roi_mode(False)

    def clear_roi(self):
        self._roi_rect = None
        self._drawing  = False
        self.update()
        self.roi_cleared.emit()

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)

        # 드래그 중 실시간 사각형
        if self._drawing:
            rect = QRect(self._start, self._end).normalized()
            painter.setPen(QPen(QColor(255, 200, 0), 2, Qt.DashLine))
            painter.setBrush(QBrush(QColor(255, 200, 0, 40)))
            painter.drawRect(rect)

        # 확정된 ROI
        if self._roi_rect:
            painter.setPen(QPen(QColor(255, 200, 0), 2))
            painter.setBrush(QBrush(QColor(255, 200, 0, 40)))
            painter.drawRect(self._roi_rect)

        painter.end()


# ──────────────────────────────────────────────
# 스타일
# ──────────────────────────────────────────────
STYLE = """
QMainWindow, QWidget#root { background-color: #0d0d14; }
QWidget { background-color: #0d0d14; color: #e0e0f0;
          font-family: 'Consolas', 'Courier New', monospace; }
QFrame#panel { background-color: #13131f; border: 1px solid #2a2a40; border-radius: 8px; }
QLabel#title { font-size: 22px; font-weight: bold; color: #00ff99; }
QLabel#count_num { font-size: 48px; font-weight: bold; color: #00ff99; }
QLabel#section_title { font-size: 11px; color: #6060a0; }
QLabel#live_badge { font-size: 10px; font-weight: bold; color: #3a3a55; }
QPushButton#roi_btn {
    background-color: #1a1a2e; color: #ffcc00;
    border: 1px solid #ffcc00; border-radius: 4px;
    padding: 4px 10px; font-size: 10px; font-family: Consolas;
}
QPushButton#roi_btn:hover { background-color: #2a2a10; }
QPushButton#roi_btn:checked { background-color: #3a3a00; color: #ffee44; }
QPushButton#clear_btn {
    background-color: #1a1a2e; color: #ff6666;
    border: 1px solid #ff6666; border-radius: 4px;
    padding: 4px 10px; font-size: 10px; font-family: Consolas;
}
QPushButton#clear_btn:hover { background-color: #2a1010; }
QTableWidget { background-color: #13131f; gridline-color: #1e1e30;
               border: none; font-size: 11px; color: #d0d0e8; }
QTableWidget::item:selected { background-color: #2a2a4a; }
QHeaderView::section { background-color: #1a1a2e; color: #00ff99;
    font-weight: bold; font-size: 11px; padding: 6px;
    border: none; border-bottom: 1px solid #2a2a40; }
QScrollBar:vertical { background: #13131f; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #3a3a55; border-radius: 4px; }
QProgressBar { background-color: #1a1a2e; border: none; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background-color: #00ff99; border-radius: 4px; }
QStatusBar { background-color: #0a0a12; color: #5050a0; font-size: 10px; }
"""


class CCTVDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AI CCTV 모니터링 대시보드")
        self.resize(1400, 760)
        self.setStyleSheet(STYLE)

        self.total_count   = 0
        self.session_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._build_ui()
        self._start_threads()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(20, 16, 20, 8)
        main_layout.setSpacing(10)

        # 타이틀
        title_row = QHBoxLayout()
        title_lbl = QLabel("● AI CCTV")
        title_lbl.setObjectName("title")
        title_row.addWidget(title_lbl)
        title_row.addStretch()
        self.status_dot = QLabel("⬤  대기 중...")
        self.status_dot.setStyleSheet("color: #3a3a55; font-size: 11px;")
        title_row.addWidget(self.status_dot)
        main_layout.addLayout(title_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2a2a40;")
        main_layout.addWidget(sep)

        content_row = QHBoxLayout()
        content_row.setSpacing(12)
        main_layout.addLayout(content_row)

        # ── 왼쪽: 카운터 + 최근 감지 이미지 ──
        left = QFrame()
        left.setObjectName("panel")
        left.setFixedWidth(320)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(14, 14, 14, 14)
        ll.setSpacing(6)

        for text, obj_name in [("감지된 사람", "section_title"), ]:
            lbl = QLabel(text)
            lbl.setObjectName(obj_name)
            lbl.setAlignment(Qt.AlignCenter)
            ll.addWidget(lbl)

        self.count_label = QLabel("0")
        self.count_label.setObjectName("count_num")
        self.count_label.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.count_label)

        unit = QLabel("명 (누적)")
        unit.setObjectName("section_title")
        unit.setAlignment(Qt.AlignCenter)
        ll.addWidget(unit)

        s2 = QFrame()
        s2.setFrameShape(QFrame.HLine)
        s2.setStyleSheet("color: #2a2a40;")
        ll.addWidget(s2)

        cap_lbl = QLabel("최근 감지 이미지")
        cap_lbl.setObjectName("section_title")
        cap_lbl.setAlignment(Qt.AlignCenter)
        ll.addWidget(cap_lbl)

        self.img_label = QLabel("이미지 없음")
        self.img_label.setFixedSize(292, 219)
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("background-color: #1a1a2e; border-radius: 4px;")
        ll.addWidget(self.img_label)

        self.last_time_label = QLabel("—")
        self.last_time_label.setObjectName("section_title")
        self.last_time_label.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.last_time_label)

        conf_lbl = QLabel("신뢰도")
        conf_lbl.setObjectName("section_title")
        conf_lbl.setAlignment(Qt.AlignCenter)
        ll.addWidget(conf_lbl)

        self.conf_bar = QProgressBar()
        self.conf_bar.setRange(0, 100)
        self.conf_bar.setValue(0)
        self.conf_bar.setTextVisible(False)
        ll.addWidget(self.conf_bar)

        self.conf_val_label = QLabel("0%")
        self.conf_val_label.setObjectName("section_title")
        self.conf_val_label.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.conf_val_label)

        ll.addStretch()
        content_row.addWidget(left)

        # ── 가운데: 실시간 스트리밍 + ROI 버튼 ──
        mid = QFrame()
        mid.setObjectName("panel")
        mid.setFixedWidth(672)
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(14, 14, 14, 14)
        ml.setSpacing(6)

        live_title_row = QHBoxLayout()
        live_title = QLabel("실시간 영상")
        live_title.setObjectName("section_title")
        live_title_row.addWidget(live_title)
        live_title_row.addStretch()

        # ROI 설정 버튼
        self.roi_btn = QPushButton("✏  ROI 설정")
        self.roi_btn.setObjectName("roi_btn")
        self.roi_btn.setCheckable(True)
        self.roi_btn.clicked.connect(self._on_roi_btn)
        live_title_row.addWidget(self.roi_btn)

        # ROI 초기화 버튼
        self.clear_btn = QPushButton("✕  ROI 초기화")
        self.clear_btn.setObjectName("clear_btn")
        self.clear_btn.clicked.connect(self._on_clear_roi)
        live_title_row.addWidget(self.clear_btn)

        self.live_badge = QLabel("● LIVE")
        self.live_badge.setObjectName("live_badge")
        live_title_row.addWidget(self.live_badge)
        ml.addLayout(live_title_row)

        # ROI 안내 라벨
        self.roi_hint = QLabel("")
        self.roi_hint.setObjectName("section_title")
        self.roi_hint.setAlignment(Qt.AlignCenter)
        ml.addWidget(self.roi_hint)

        self.live_label = LiveViewLabel()
        self.live_label.setFixedSize(632, 474)
        self.live_label.setAlignment(Qt.AlignCenter)
        self.live_label.setStyleSheet("background-color: #0a0a1a; border-radius: 4px;")
        self.live_label.setText("연결 대기 중...")
        self.live_label.roi_set.connect(self._on_roi_set)
        self.live_label.roi_cleared.connect(self._on_roi_cleared)
        ml.addWidget(self.live_label)

        ml.addStretch()
        content_row.addWidget(mid)

        # ── 오른쪽: 로그 테이블 ──
        right = QFrame()
        right.setObjectName("panel")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(6)

        log_lbl = QLabel("감지 로그")
        log_lbl.setObjectName("section_title")
        rl.addWidget(log_lbl)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "감지 시각", "신뢰도", "파일명"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 45)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 75)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        rl.addWidget(self.table)

        content_row.addWidget(right)

        self.statusBar().showMessage(
            f"  이벤트:{EVENT_PORT}  스트리밍:{STREAM_PORT}  ROI:{ROI_PORT}"
            f"  |  세션 시작: {self.session_start}"
        )

    def _start_threads(self):
        self.event_thread = EventServerThread()
        self.event_thread.event_received.connect(self._on_event)
        self.event_thread.start()

        self.stream_thread = StreamReceiverThread()
        self.stream_thread.frame_received.connect(self._on_frame)
        self.stream_thread.start()

    # ── ROI 버튼 핸들러 ──
    def _on_roi_btn(self, checked):
        self.live_label.set_roi_mode(checked)
        if checked:
            self.roi_hint.setText("📌 영상 위에서 마우스로 드래그해 ROI 영역을 설정하세요")
            self.roi_hint.setStyleSheet("color: #ffcc00; font-size: 10px;")
        else:
            self.roi_hint.setText("")

    def _on_clear_roi(self):
        self.live_label.clear_roi()
        self.roi_btn.setChecked(False)
        self.roi_hint.setText("")

    def _on_roi_set(self, points):
        """ROI 확정 → 라즈베리파이로 전송"""
        self.roi_btn.setChecked(False)
        self.roi_hint.setText(f"✅ ROI 설정 완료: {points[0]} ~ {points[2]}")
        self.roi_hint.setStyleSheet("color: #00ff99; font-size: 10px;")
        threading.Thread(target=send_roi_to_rpi, args=(points,), daemon=True).start()

    def _on_roi_cleared(self):
        """ROI 초기화 → 라즈베리파이로 전송"""
        self.roi_hint.setText("🔄 ROI 초기화 - 전체 화면 감지 중")
        self.roi_hint.setStyleSheet("color: #6060a0; font-size: 10px;")
        threading.Thread(target=send_roi_to_rpi, args=(None,), daemon=True).start()

    def _on_frame(self, data: bytes):
        img = QImage.fromData(data)
        pixmap = QPixmap.fromImage(img).scaled(
            632, 474, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.live_label.setPixmap(pixmap)
        self.live_badge.setStyleSheet("color: #ff4444; font-size: 10px; font-weight: bold;")

    def _on_event(self, timestamp, confidence, count, image_bytes, filename):
        self.total_count += count
        self.count_label.setText(str(self.total_count))

        img = QImage.fromData(image_bytes)
        pixmap = QPixmap.fromImage(img).scaled(
            292, 219, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.img_label.setPixmap(pixmap)
        self.last_time_label.setText(timestamp)

        pct = int(confidence * 100)
        self.conf_bar.setValue(pct)
        self.conf_val_label.setText(f"{pct}%")

        self.status_dot.setText(f"⬤  마지막 감지: {timestamp}")
        self.status_dot.setStyleSheet("color: #00ff99; font-size: 11px;")

        self.table.insertRow(0)
        for col, text in enumerate([str(self.total_count), timestamp,
                                     f"{confidence:.2%}", filename]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            if col == 2:
                item.setForeground(QColor("#00ff99"))
            self.table.setItem(0, col, item)

        while self.table.rowCount() > MAX_LOG_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)

        self.statusBar().showMessage(
            f"  이벤트:{EVENT_PORT}  스트리밍:{STREAM_PORT}  ROI:{ROI_PORT}"
            f"  |  세션: {self.session_start}"
            f"  |  총 감지: {self.total_count}건"
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = CCTVDashboard()
    win.show()
    sys.exit(app.exec_())