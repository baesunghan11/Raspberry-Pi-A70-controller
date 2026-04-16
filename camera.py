"""
FLIR A70 카메라 제어 — HTTP + Radiometric JPEG 방식
Spinnaker SDK 불필요. 네트워크(GigE)를 통해 카메라 IP로 직접 접근.

카메라 준비:
  - FLIR A70의 IP를 고정 설정 (예: 192.168.0.100)
  - 카메라 웹 인터페이스에서 "Radiometric JPEG" 스트리밍 활성화

의존성: requests, flirpy, numpy, opencv-python
"""

import io
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests

try:
    from flirpy.image.thermal import ThermalImage
    _FLIRPY = True
except ImportError:
    _FLIRPY = False
    print("[WARNING] flirpy 없음 — pip install flirpy")


def _log(msg: str):
    print(f"[Camera] {msg}")


# FLIR A70 HTTP 스냅샷 엔드포인트 후보 (카메라 펌웨어에 따라 다름)
_SNAPSHOT_ENDPOINTS = [
    "/snapshot.jpg",
    "/image/snapshot.jpg",
    "/cgi-bin/snapshot.jpg",
    "/images/snapshot",
    "/thermal/snapshot.jpg",
]


class FlirA70Camera:
    """
    HTTP + Radiometric JPEG 방식으로 FLIR A70 제어.

    동작 방식:
      1. requests로 카메라 HTTP 서버에서 R-JPEG 이미지 취득
      2. flirpy.image.thermal.ThermalImage로 온도 배열 추출
      3. 실화상은 R-JPEG 내 임베드된 미리보기 이미지 사용
    """

    def __init__(self):
        self.ip: str | None = None
        self.port: int = 80
        self.snapshot_url: str | None = None   # 성공한 스냅샷 URL 캐시
        self._session = requests.Session()
        self._session.headers.update({"Connection": "keep-alive"})
        self._connected = False

    # ──────────────────────────────────────────────
    # 연결 / 해제
    # ──────────────────────────────────────────────
    def connect(self, ip: str, port: int = 80,
                snapshot_path: str | None = None) -> bool:
        """
        카메라 HTTP 서버에 연결.

        Args:
            ip:             카메라 IP 주소 (예: "192.168.0.100")
            port:           HTTP 포트 (기본 80)
            snapshot_path:  R-JPEG 스냅샷 경로 (None이면 자동 탐색)
        """
        self.ip   = ip
        self.port = port
        base_url  = f"http://{ip}:{port}" if port != 80 else f"http://{ip}"

        # 연결 가능 여부 테스트
        try:
            self._session.get(base_url + "/", timeout=3)
        except requests.exceptions.ConnectionError:
            _log(f"연결 실패 — {base_url} 에 도달할 수 없음")
            return False
        except Exception:
            pass  # 404나 다른 오류여도 일단 진행

        # 스냅샷 엔드포인트 결정
        if snapshot_path:
            self.snapshot_url = base_url + snapshot_path
        else:
            self.snapshot_url = self._find_snapshot_endpoint(base_url)

        if not self.snapshot_url:
            _log("스냅샷 엔드포인트를 찾지 못했습니다. "
                 "카메라 웹 인터페이스에서 HTTP 스트리밍을 활성화하세요.")
            return False

        self._connected = True
        _log(f"연결 성공: {self.snapshot_url}")
        return True

    def _find_snapshot_endpoint(self, base_url: str) -> str | None:
        """사용 가능한 스냅샷 엔드포인트 자동 탐색"""
        for ep in _SNAPSHOT_ENDPOINTS:
            url = base_url + ep
            try:
                r = self._session.get(url, timeout=3)
                if r.status_code == 200 and len(r.content) > 500:
                    _log(f"스냅샷 엔드포인트 발견: {ep}")
                    return url
            except Exception:
                continue
        return None

    def disconnect(self):
        self._connected = False
        try:
            self._session.close()
        except Exception:
            pass
        _log("연결 해제")

    @property
    def connected(self) -> bool:
        return self._connected

    # ──────────────────────────────────────────────
    # Radiometric JPEG 취득
    # ──────────────────────────────────────────────
    def fetch_rjpeg(self, timeout: int = 5) -> bytes | None:
        """HTTP GET으로 카메라에서 Radiometric JPEG 취득"""
        try:
            r = self._session.get(self.snapshot_url, timeout=timeout)
            if r.status_code == 200 and len(r.content) > 500:
                return r.content
            _log(f"스냅샷 응답 오류: HTTP {r.status_code}")
        except requests.exceptions.Timeout:
            _log("스냅샷 타임아웃")
        except Exception as e:
            _log(f"스냅샷 취득 실패: {e}")
        return None

    # ──────────────────────────────────────────────
    # Radiometric JPEG 파싱
    # ──────────────────────────────────────────────
    @staticmethod
    def parse_rjpeg(jpeg_bytes: bytes) -> tuple:
        """
        Radiometric JPEG → (temp_celsius_f32, visual_bgr)

        flirpy로 FLIR 전용 EXIF에서 Planck 공식 기반 온도 계산.
        Returns:
            temp    : np.ndarray float32, shape (H, W), 단위 °C
            visual  : np.ndarray uint8 BGR, 또는 None
        """
        if not _FLIRPY:
            _log("flirpy 없음 — 온도 파싱 불가")
            return None, None

        # 임시 파일로 저장 후 flirpy 파싱
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(jpeg_bytes)
            tmp_path = f.name
        try:
            ti = ThermalImage(tmp_path)

            # 온도 배열 (°C)
            temp = None
            for attr in ("thermal_image", "get_temperature"):
                if hasattr(ti, attr):
                    val = getattr(ti, attr)
                    temp = val() if callable(val) else val
                    break
            if temp is None:
                _log("ThermalImage에서 온도 데이터를 읽지 못했습니다")
                return None, None
            temp = np.asarray(temp, dtype=np.float32)

            # 실화상 미리보기 (R-JPEG 내 임베드)
            visual = None
            for attr in ("preview_image", "thermal_preview"):
                if hasattr(ti, attr):
                    v = getattr(ti, attr)
                    if v is not None:
                        v = np.asarray(v, dtype=np.uint8)
                        # RGB → BGR 변환
                        if v.ndim == 3 and v.shape[2] == 3:
                            visual = cv2.cvtColor(v, cv2.COLOR_RGB2BGR)
                        else:
                            visual = v
                        break

            return temp, visual

        except Exception as e:
            _log(f"R-JPEG 파싱 실패: {e}")
            return None, None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ──────────────────────────────────────────────
    # 촬영 인터페이스
    # ──────────────────────────────────────────────
    def capture_all(self) -> tuple:
        """
        단일 캡처.
        Returns: (jpeg_bytes, temp_celsius, width, height, visual_bgr)
                 실패 시 (None, None, 0, 0, None)
        """
        jpeg = self.fetch_rjpeg()
        if jpeg is None:
            return None, None, 0, 0, None

        temp, visual = self.parse_rjpeg(jpeg)
        if temp is None:
            return None, None, 0, 0, None

        h, w = temp.shape[:2]
        return jpeg, temp, w, h, visual

    def get_frame(self) -> tuple:
        """
        스트리밍용 단일 프레임.
        Returns: (temp_celsius, visual_bgr) 또는 (None, None)
        """
        jpeg = self.fetch_rjpeg(timeout=3)
        if jpeg is None:
            return None, None
        return self.parse_rjpeg(jpeg)

    # ──────────────────────────────────────────────
    # 저장
    # ──────────────────────────────────────────────
    def save_capture(self, jpeg_bytes: bytes, temp: np.ndarray,
                     visual: "np.ndarray | None",
                     save_dir: Path,
                     arduino_data: "dict | None" = None,
                     prefix: str = "capture") -> dict:
        """촬영 결과를 save_dir에 저장. 저장된 파일 경로 dict 반환."""
        save_dir.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = str(save_dir / f"{prefix}_{ts}")
        saved = {}

        # 원본 Radiometric JPEG
        rjpeg_path = base + "_radiometric.jpg"
        with open(rjpeg_path, "wb") as f:
            f.write(jpeg_bytes)
        saved["radiometric_jpg"] = rjpeg_path
        _log(f"저장: {rjpeg_path}")

        # 온도 CSV
        csv_path = base + "_temperature.csv"
        np.savetxt(csv_path, temp, delimiter=",", fmt="%.2f")
        saved["temp_csv"] = csv_path
        _log(f"저장: {csv_path}")

        # 컬러맵 PNG (min-max 정규화)
        span = float(temp.max() - temp.min()) or 1.0
        norm = ((temp - temp.min()) / span * 255).astype(np.uint8)
        cmap = cv2.applyColorMap(norm, cv2.COLORMAP_JET)
        cmap_path = base + "_colormap.png"
        cv2.imwrite(cmap_path, cmap)
        saved["colormap"] = cmap_path
        _log(f"저장: {cmap_path}")

        # 실화상 PNG
        if visual is not None:
            vis_path = base + "_visual.png"
            cv2.imwrite(vis_path, visual)
            saved["visual"] = vis_path
            _log(f"저장: {vis_path}")

        # 메타데이터 JSON
        meta = {
            "timestamp":   ts,
            "camera_ip":   self.ip,
            "width":       int(temp.shape[1]),
            "height":      int(temp.shape[0]),
            "temp_min":    round(float(temp.min()), 2),
            "temp_max":    round(float(temp.max()), 2),
            "temp_mean":   round(float(temp.mean()), 2),
            "temp_median": round(float(np.median(temp)), 2),
            "has_visual":  visual is not None,
        }
        if arduino_data:
            meta["arduino"] = arduino_data
        meta_path = base + "_metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        saved["metadata"] = meta_path

        return saved
