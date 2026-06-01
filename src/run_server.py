"""
run_server.py — 데스크톱 진입점 (PyInstaller onedir 번들)

폐쇄망 데모 PC 에서 더블클릭 실행 → 로컬 uvicorn(:8021) 기동 후 기본 브라우저 자동 오픈.
오프라인 강제(HF/Transformers 네트워크 시도 0). storage·모델은 paths 모듈이 frozen 인지 해석.

개발 모드에서도 동일하게 동작: python src/run_server.py
"""
import os
import threading
import webbrowser

# 임베딩 모델 오프라인 강제(번들 동봉 모델만 사용 — 외부 다운로드 시도 차단).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("USE_OLLAMA", "auto")  # Ollama 가동 시 자동 사용, 없으면 비활성

import uvicorn  # noqa: E402
from app import app  # noqa: E402  — paths 가 frozen storage/static/model 경로 해석

HOST, PORT = "127.0.0.1", 8021


def _open_browser():
    """서버가 뜬 직후 기본 브라우저로 대시보드 오픈(약간의 기동 지연 후)."""
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Timer(2.5, _open_browser).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
