# -*- mode: python ; coding: utf-8 -*-
"""
prism_fs.spec — PyInstaller onedir 풀번들 (prism-fs)

torch + sentence-transformers + ko-sroberta 모델을 함께 묶어 폐쇄망에서 개발환경과
동일한 AI 검색·주제매핑·RAG 동작을 보장한다(인덱스 768차원 ↔ bigram 512차원 불일치 회피).
산출물 이름은 VERSION(PRISM_VERSION 환경변수) 으로 자동 주입: setup_v{VERSION}.

빌드: build.ps1 또는 직접 `pyinstaller prism_fs.spec --distpath dist\\setup --workpath build\\work`
"""
import os
from PyInstaller.utils.hooks import collect_all

VERSION = os.environ.get("PRISM_VERSION", "0.1.0")
NAME = f"setup_v{VERSION}"

# 번들 동봉 리소스: 정적 UI(vendored pdf.js 포함) + 임베딩 모델.
datas = [
    ("src/static", "static"),
    ("src/models/ko-sroberta", "models/ko-sroberta"),
]
binaries = []
# 앱 모듈 + uvicorn 런타임 서브모듈(동적 import 라 명시 필요).
hiddenimports = [
    "app", "paths", "safety", "fs_compare", "notes_rag",
    "note_filters", "note_topics", "collect_dart",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

# 무거운 패키지는 데이터·바이너리·서브모듈 일괄 수집(누락 시 frozen 기동 실패 방지).
for pkg in ("sentence_transformers", "transformers", "torch", "tokenizers",
            "safetensors", "fitz", "rank_bm25", "sklearn", "scipy",
            "huggingface_hub", "fastapi", "starlette"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # 미설치 패키지는 건너뜀(예: sklearn/scipy 선택적)

a = Analysis(
    ["src/run_server.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "notebook", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 폐쇄망 로그 가시성 유지
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
