"""
paths.py — 경로 해석 중앙화 (prism-fs)

개발 모드와 PyInstaller frozen(onedir) 모드에서 storage·static·모델 경로를 일관되게 해석한다.

frozen 모드 문제: PyInstaller 가 모듈을 임시 추출 폴더(sys._MEIPASS)에서 실행하므로 각 모듈의
`__file__` 은 번들 내부를 가리킨다 → 영속(쓰기) storage 가 임시 폴더로 잘못 잡힌다.
따라서 **번들 리소스(읽기: static·모델)** 와 **exe 옆 영속 데이터(쓰기: storage)** 를 분리한다.

- BUNDLE_DIR : 번들된 정적 리소스 루트(frozen=_MEIPASS, dev=src/).
- APP_DIR    : 실행 파일 위치(frozen=exe 폴더, dev=src/) — storage·.env 의 영속 기준.
- STORAGE_ROOT 는 PRISM_STORAGE 환경변수로 재정의 가능(테스트·이식 대비).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):  # PyInstaller 번들 실행
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    APP_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

STATIC_DIR = BUNDLE_DIR / "static"
MODEL_DIR = BUNDLE_DIR / "models" / "ko-sroberta"

# storage 는 쓰기 영속(수집·인덱싱 결과) → exe 옆. 환경변수로 외부 지정 가능.
STORAGE_ROOT = Path(os.getenv("PRISM_STORAGE") or (APP_DIR / "storage"))
LIBRARY_ROOT = STORAGE_ROOT / "library"
ENV_PATH = APP_DIR / ".env"
