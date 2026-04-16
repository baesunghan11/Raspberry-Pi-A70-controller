# Raspberry Pi A70 Controller

FLIR A70 열화상 카메라와 Arduino Uno 센서 데이터를 수집하는 Raspberry Pi 5용 웹 기반 제어 프로그램입니다.
Spinnaker SDK 없이 HTTP + Radiometric JPEG 방식으로 카메라에 접근합니다.

---

## 주요 기능

- 열화상 / 실화상 캡처
- 실시간 스트리밍
- 설정 간격 자동 촬영
- Arduino 온습도 센서 데이터 수신 (TC1, TC2, CJ1, CJ2, AirT, RH)
- 사각형 ROI 온도 통계 (평균 / 최고 / 최저 / 중앙값)

---

## 필요 환경

- Raspberry Pi 5
- Python 3.10 이상
- FLIR A70 (GigE Vision, Radiometric JPEG 활성화)
- Arduino Uno (MAX31856 + DHT22)

---

## 라이브러리

| 라이브러리 | 버전 | 용도 |
|---|---|---|
| [fastapi](https://fastapi.tiangolo.com) | ≥ 0.110.0 | 웹 서버 프레임워크 |
| [uvicorn](https://www.uvicorn.org) | ≥ 0.29.0 | ASGI 서버 |
| [numpy](https://numpy.org) | ≥ 1.26.0 | 온도 배열 연산 |
| [opencv-python](https://opencv.org) | ≥ 4.9.0 | 이미지 처리 / 컬러맵 |
| [pyserial](https://pyserial.readthedocs.io) | ≥ 3.5 | Arduino 시리얼 통신 |
| [jinja2](https://jinja.palletsprojects.com) | ≥ 3.1.0 | HTML 템플릿 렌더링 |
| [python-multipart](https://github.com/Kludex/python-multipart) | ≥ 0.0.9 | 파일 업로드 파싱 |
| [requests](https://requests.readthedocs.io) | ≥ 2.31.0 | 카메라 HTTP 통신 |
| [flirpy](https://github.com/LJMUAstroecology/flirpy) | ≥ 0.4.0 | Radiometric JPEG 온도 파싱 |

설치:
```bash
pip install -r requirements.txt
```

---

## 실행 방법

```bash
python web_server.py
```

브라우저에서 `http://<라즈베리파이 IP>:8000` 접속

---

## 카메라 설정

1. FLIR A70 IP 고정 설정 (예: `192.168.0.100`)
2. 카메라 웹 인터페이스에서 **Radiometric JPEG 스트리밍 활성화**
3. 웹 UI에서 카메라 IP 입력 후 연결

---

## Arduino 데이터 형식

Arduino는 아래 CSV 형식으로 9600 baud 시리얼 출력:

```
YYYY-MM-DD HH:MM:SS, TC1, CJ1, TC2, CJ2, AirT, RH
```

| 항목 | 센서 | 단위 |
|---|---|---|
| TC1, TC2 | MAX31856 열전대 | °C |
| CJ1, CJ2 | MAX31856 냉접점 | °C |
| AirT | DHT22 | °C |
| RH | DHT22 | % |
