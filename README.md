# 🎥 AI CCTV 프로젝트

YOLOv8 기반 실시간 사람 감지 CCTV 시스템.  
라즈베리파이5 + AI 카메라로 감지 후 Windows PC로 전송하여 모니터링합니다.

---

## 📐 시스템 아키텍처

```
[라즈베리파이5 + IMX500 AI 카메라]
  picamera2로 프레임 캡처 (640x480, RGB888)
  → YOLOv8n 추론 (person 클래스만)
  → ROI 영역 내 객체 필터링
  → TCP 소켓으로 Windows 전송

          ↓ 192.168.0.xxx

[Windows PC - PyQt5 대시보드]
  실시간 라이브 뷰 (포트 9998)
  감지 이벤트 수신 (포트 9999)
  ROI 좌표 전송   (포트 9997)
  → captures/ 이미지 저장
  → detections.csv 로그 기록
```

---

## 🗂 파일 구성

```
AI_CCTV/
├── rpi_sender.py         # 라즈베리파이5 실행 파일
├── windows_receiver.py   # Windows PC 실행 파일 (GUI)
├── captures/             # 감지 이미지 저장 폴더 (자동 생성)
├── detections.csv        # 감지 로그 (자동 생성)
└── README.md
```

---

## 🔌 포트 구성

| 포트 | 방향 | 용도 |
|------|------|------|
| 9999 | 라즈베리파이 → Windows | 감지 이벤트 (이미지 + 메타데이터) |
| 9998 | 라즈베리파이 → Windows | 실시간 스트리밍 (15fps) |
| 9997 | Windows → 라즈베리파이 | ROI 좌표 전송 |

---

## ⚙️ 환경 구성

### 라즈베리파이5

**OS:** Raspberry Pi OS 64-bit  
**Python:** 3.13 (conda 환경)

```bash
# 1. 시스템 패키지 설치
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-numpy imx500-all

# 2. Miniconda 설치
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh
bash Miniconda3-latest-Linux-aarch64.sh

# 3. conda 환경 생성
conda create -n ai_cctv python=3.13 -y
conda activate ai_cctv

# 4. picamera2 심링크 연결
SITE=$(python -c "import site; print(site.getsitepackages()[0])")
ln -s /usr/lib/python3/dist-packages/picamera2 $SITE/picamera2
ln -s /usr/lib/python3/dist-packages/libcamera $SITE/libcamera
ln -s /usr/lib/python3/dist-packages/pykms     $SITE/pykms
ln -s /usr/lib/python3/dist-packages/v4l2      $SITE/v4l2
echo "/usr/lib/python3/dist-packages" > $SITE/system_packages.pth

# 5. YOLOv8 설치
pip install ultralytics
```

### Windows PC

**Python:** 3.10 (conda 환경)

```powershell
# 1. conda 환경 생성
conda create -n ai_cctv python=3.10 -y
conda activate ai_cctv

# 2. 패키지 설치
pip install PyQt5

# 3. 방화벽 포트 허용
netsh advfirewall firewall add rule name="AI_CCTV_EVENT"  protocol=TCP dir=in localport=9999 action=allow
netsh advfirewall firewall add rule name="AI_CCTV_STREAM" protocol=TCP dir=in localport=9998 action=allow
netsh advfirewall firewall add rule name="AI_CCTV_ROI"    protocol=TCP dir=in localport=9997 action=allow
```

---

## 🚀 실행 방법

### 1. IP 설정

`rpi_sender.py` 상단 수정:
```python
WINDOWS_HOST = "192.168.0.6"   # Windows PC IP (ipconfig로 확인)
```

`windows_receiver.py` 상단 수정:
```python
RPI_HOST = "192.168.0.48"      # 라즈베리파이 IP
```

### 2. 파일 전송 (Windows → 라즈베리파이)

```powershell
scp rpi_sender.py sol@192.168.0.48:~/rpi_sender.py
```

### 3. 실행 순서

**① Windows 먼저 실행**
```powershell
conda activate ai_cctv
python windows_receiver.py
```

**② 라즈베리파이 실행**
```bash
conda activate ai_cctv
python rpi_sender.py
```

---

## 🖥️ GUI 기능

| 기능 | 설명 |
|------|------|
| 실시간 라이브 뷰 | 라즈베리파이 카메라 영상 15fps 표시 |
| 감지 카운터 | 누적 감지 인원 수 표시 |
| 최근 감지 이미지 | 마지막으로 감지된 프레임 표시 |
| 신뢰도 바 | 감지 신뢰도 시각화 |
| 감지 로그 테이블 | 시각 / 신뢰도 / 파일명 기록 |
| ROI 설정 | 마우스 드래그로 감지 영역 지정 |
| ROI 초기화 | 전체 화면 감지로 복귀 |

---

## 📁 저장 포맷

### 이미지
```
captures/capture_20250317_143201.jpg
```

### CSV 로그
```csv
timestamp,image_filename,confidence
2025-03-17 14:32:01,capture_20250317_143201.jpg,0.9200
2025-03-17 14:32:07,capture_20250317_143207.jpg,0.8810
```

---

## 🔧 주요 설정값

### rpi_sender.py
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `WINDOWS_HOST` | `192.168.0.6` | Windows PC IP ← 필수 수정 |
| `COOLDOWN_SEC` | `3.0` | 감지 후 재전송 대기 시간(초) |
| `CONFIDENCE_THRESHOLD` | `0.50` | 최소 신뢰도 (0~1) |
| `MODEL_PATH` | `yolov8n.pt` | nano=빠름 / yolov8s.pt=정확도↑ |
| `STREAM_FPS` | `15` | 스트리밍 프레임 레이트 |

### windows_receiver.py
| 변수 | 기본값 | 설명 |
|------|--------|------|
| `RPI_HOST` | `192.168.0.48` | 라즈베리파이 IP ← 필수 수정 |
| `SAVE_DIR` | `captures` | 이미지 저장 폴더 |
| `CSV_FILE` | `detections.csv` | 로그 파일명 |

---

## 🐛 트러블슈팅

### picamera2 import 오류
```bash
# 심링크 재설정
SITE=$(python -c "import site; print(site.getsitepackages()[0])")
ln -s /usr/lib/python3/dist-packages/picamera2 $SITE/picamera2
```

### 색상 이상 (파란색 계열)
- `picamera2`는 `RGB888` 포맷으로 캡처
- `cv2.imencode` 에 변환 없이 그대로 전달해야 정상 색상 출력
- `cvtColor(RGB2BGR)` 변환 사용 시 색 반전 발생

### SSH 접속 오류 (host key changed)
```powershell
ssh-keygen -R 192.168.0.48
```

### ROI 수신 오류
- Windows와 라즈베리파이의 ROI 데이터 포맷이 일치해야 함
- 형식: `{"roi": [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]}` 또는 `{"roi": null}`

---

## 📦 의존성

### 라즈베리파이
| 패키지 | 설치 방법 |
|--------|----------|
| picamera2 | `sudo apt install python3-picamera2` |
| opencv | `sudo apt install python3-opencv` |
| ultralytics | `pip install ultralytics` |

### Windows
| 패키지 | 설치 방법 |
|--------|----------|
| PyQt5 | `pip install PyQt5` |