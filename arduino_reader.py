"""
Arduino Uno 시리얼 데이터 수신기
출력 형식: 'YYYY-MM-DD HH:MM:SS, TC1, CJ1, TC2, CJ2, AirT, RH'
  - TC1, TC2  : MAX31856 열전대 온도 (°C)
  - CJ1, CJ2  : 냉접점 온도 (°C)
  - AirT       : DHT22/CM2305 기온 (°C)
  - RH         : 상대습도 (%)
"""

import os
import threading
import time
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
    _SERIAL = True
except ImportError:
    _SERIAL = False
    print("[WARNING] pyserial이 없습니다. pip install pyserial")


def find_arduino_port() -> "str | None":
    """Arduino Uno 포트 자동 탐색"""
    if not _SERIAL:
        return None
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if any(k in desc or k in hwid for k in
               ["arduino", "uno", "ch340", "ch341", "2341:0043", "2341:0001"]):
            return p.device
    # RPi 환경 기본 후보
    for candidate in ["/dev/ttyACM0", "/dev/ttyACM1",
                      "/dev/ttyUSB0", "/dev/ttyUSB1"]:
        if os.path.exists(candidate):
            return candidate
    return None


class ArduinoReader:
    """백그라운드 스레드로 Arduino 시리얼 데이터를 지속 수신"""

    def __init__(self):
        self._serial = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._history: list[dict] = []
        self.port: str | None = None
        self.baudrate: int = 9600

    # ──────────────────────────────────────────
    # 연결 / 해제
    # ──────────────────────────────────────────
    def connect(self, port: str | None = None, baudrate: int = 9600) -> bool:
        if not _SERIAL:
            return False
        self.baudrate = baudrate
        self.port = port or find_arduino_port()
        if not self.port:
            print("[Arduino] 포트를 찾을 수 없습니다.")
            return False
        try:
            self._serial = serial.Serial(self.port, baudrate, timeout=3)
            time.sleep(2)           # Arduino 리셋 대기
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            print(f"[Arduino] 연결 성공: {self.port} @ {baudrate} baud")
            return True
        except Exception as e:
            print(f"[Arduino] 연결 실패: {e}")
            self._serial = None
            return False

    def disconnect(self):
        self._running = False
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        print("[Arduino] 연결 해제")

    # ──────────────────────────────────────────
    # 수신 루프
    # ──────────────────────────────────────────
    def _loop(self):
        while self._running:
            try:
                raw_line = self._serial.readline()
                line = raw_line.decode("utf-8", errors="ignore").strip()
                # 헤더/오류 줄 건너뜀
                if not line or not line[0].isdigit():
                    continue
                data = self._parse(line)
                if data:
                    with self._lock:
                        self._latest = data
                        self._history.append(data)
                        if len(self._history) > 200:
                            self._history.pop(0)
            except Exception:
                time.sleep(0.5)

    # ──────────────────────────────────────────
    # 파싱
    # ──────────────────────────────────────────
    @staticmethod
    def _parse(line: str) -> "dict | None":
        """'YYYY-MM-DD HH:MM:SS, TC1, CJ1, TC2, CJ2, AirT, RH' → dict"""
        try:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                return None

            def to_f(s: str) -> "float | None":
                s = s.strip()
                return None if s in ("nan", "inf", "-inf", "") else float(s)

            return {
                "time":      parts[0],
                "tc1":       to_f(parts[1]),
                "cj1":       to_f(parts[2]),
                "tc2":       to_f(parts[3]),
                "cj2":       to_f(parts[4]),
                "air_temp":  to_f(parts[5]),
                "humidity":  to_f(parts[6]),
                "received":  datetime.now().isoformat(timespec="seconds"),
            }
        except Exception:
            return None

    # ──────────────────────────────────────────
    # 데이터 조회
    # ──────────────────────────────────────────
    def get_latest(self) -> "dict | None":
        with self._lock:
            return dict(self._latest) if self._latest else None

    def get_history(self, n: int = 50) -> list:
        with self._lock:
            return list(self._history[-n:])

    @property
    def connected(self) -> bool:
        return self._running and self._serial is not None
