"""
4대 금융지주 주석 비교 에이전트 — FastAPI 백엔드 (PoC v2 · 라이브러리 모델, LLM API 미사용)

특징:
- 영속 라이브러리 (회사 × 연도분기)
- 임베딩 백엔드 자동 감지: 로컬 sentence-transformers > API > bigram fallback
- BM25 하이브리드 매칭 (가중치 0.7 임베딩 + 0.3 BM25)
- LLM API 키 없이도 정상 동작

실행:
    pip install -r requirements.txt
    # (권장) 로컬 임베딩 모델 사전 다운로드:
    #   python -c "from sentence_transformers import SentenceTransformer; \\
    #              SentenceTransformer('jhgan/ko-sroberta-multitask').save('./models/ko-sroberta')"
    #   export EMBED_MODEL_PATH=./models/ko-sroberta
    uvicorn main:app --reload --port 8000

API 엔드포인트:
    POST   /api/library/upload                    - 단일 (회사, 기간) PDF 업로드
    GET    /api/library                            - 전체 카탈로그 매트릭스 조회
    GET    /api/library/{company}/{period}        - 단일 항목 메타
    DELETE /api/library/{company}/{period}        - 단일 항목 삭제
    POST   /api/library/index/{company}/{period}  - (재)인덱싱 시작
    GET    /api/library/index/status              - 전체 인덱싱 상태 매트릭스
    POST   /api/compare                            - 비교 대상 명시 후 검색
    GET    /api/pdf?company=&period=               - PDF 스트리밍 (PDF.js 사용)
"""

import os
import re
import sys
import json
import shutil
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from contextlib import asynccontextmanager
from urllib.parse import quote

import fitz  # PyMuPDF
import httpx
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import collect_dart as cdart  # DART 수집 로직 재사용(동일 storage 레이아웃)
import fs_compare  # 재무제표 결정론 비교 엔진(증감/연결vs별도/벤치/비율 + provenance)
import notes_rag  # 주석 RAG(§5.4, 옵트인) — 정성 텍스트 전용, 숫자 무경유·인용 강제
import note_filters  # 주석 종류(주기/서술형) 결정론 필터
import note_topics  # §5.2 표준 주제 매핑(임베딩 분류, AI 무경유)
import safety  # 시크릿 마스킹·provenance 중앙화
import synonyms  # 회계 동의어 쿼리 확장(BM25/lexical, 결정론)

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
# CWD/frozen 비의존: paths 모듈이 dev·PyInstaller 양쪽 경로를 일관 해석.
import paths  # noqa: E402
BASE_DIR = paths.BUNDLE_DIR
STORAGE_ROOT = paths.STORAGE_ROOT
LIBRARY_ROOT = paths.LIBRARY_ROOT
CATALOG_PATH = STORAGE_ROOT / "catalog.json"
# Step 7: 주제사전 자동초안(build_topic_dict.py 산출). 있으면 coverage 토픽 소스로 사용.
TOPIC_DICT_PATH = STORAGE_ROOT / "topic_dict.json"
LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)

VALID_COMPANIES = {"신한", "KB", "하나", "우리"}
PERIOD_PATTERN = re.compile(r"^\d{4}(Q[1-4]|FY)$")

# 매칭 임계값 (env 오버라이드 가능) — 순수 규칙, AI 미사용.
# 단일 점수만으로는 오답/정답 구분 불가(리스→사채 0.7 vs 공정가치 0.735)하여
# 점수 플로어 + 어휘 일치(lexical_hit) 두 신호를 함께 사용한다.
MIN_MATCH_SCORE = float(os.getenv("MIN_MATCH_SCORE", "0.45"))  # 최소 채택 점수
LEXICAL_FLOOR   = float(os.getenv("LEXICAL_FLOOR", "0.30"))    # 어휘 일치 시 완화 하한
HIGH_CONF       = float(os.getenv("HIGH_CONF", "0.65"))        # 고신뢰 하한

# B-1/B-2 (note_kind 인지 검색): 주기(회계정책·작성기준·일반사항 등 prose) 주석은 제목·본문
# 키워드가 빈약해 BM25·lexical 신호가 약하고 XBRL 미태깅이 많다(진단 pdf_only로 확인). 따라서
#   ① 하이브리드 가중을 임베딩(의미매칭) 쪽으로 올리고 ② 채택 임계를 완화한다.
# 서술형(항목공시: 파생상품·대출채권 등 표·키워드 신호 보유)은 현행 유지(0.7/0.3·0.45) — 무회귀.
COS_W_DEFAULT    = float(os.getenv("COS_W_DEFAULT", "0.7"))    # 서술형·기타: 임베딩 가중
BM_W_DEFAULT     = float(os.getenv("BM_W_DEFAULT", "0.3"))     # 서술형·기타: BM25 가중
COS_W_POLICY     = float(os.getenv("COS_W_POLICY", "0.85"))    # 주기(정책 서술): 의미매칭 비중↑
BM_W_POLICY      = float(os.getenv("BM_W_POLICY", "0.15"))
POLICY_MIN_MATCH = float(os.getenv("POLICY_MIN_MATCH", "0.40"))  # 주기 채택 하한(완화)

# Step 4 — 신한 인사이트(커버리지/차집합)용 상수
# DEFAULT_TOPICS: 횡단 커버리지 매트릭스 기본 주제 목록.
#   주의: 회계팀 확정 Top 주제가 아닌 "플레이스홀더" — 확정 리스트로 교체 전제.
DEFAULT_TOPICS = [
    "공정가치", "대손충당금", "금융상품 위험", "영업권", "리스",
    "확정급여", "법인세", "우발부채 및 약정", "특수관계자 거래", "자본",
]
# GAP_SIM_THRESHOLD: 구조 차집합(B)에서 두 회사의 note 가 "대응"되는지 판정할 코사인 하한.
#   한국어 임베딩 anisotropy(무관 쌍 baseline ~0.3-0.5, 진짜 매치 0.7+)를 고려해 실데이터로 보정.
GAP_SIM_THRESHOLD = float(os.getenv("GAP_SIM_THRESHOLD", "0.62"))

# 회사·기간별 인덱싱 상태 (key: "{company}/{period}")
INDEX_STATUS: Dict[str, Dict[str, Any]] = {}

# 회사·기간별 DART 수집 상태 (key: "{company}/{period}")
COLLECT_STATUS: Dict[str, Dict[str, Any]] = {}

# 영문 Word 매핑 작업 상태 (key: job_id)
WORDMAP_STATUS: Dict[str, Dict[str, Any]] = {}

# 라이브러리 period 접미 → DART reprt_code (Q4 는 DART 미지원).
_SUFFIX_TO_REPRT = {"Q1": "11013", "Q2": "11012", "Q3": "11014", "FY": "11011"}

# 임베딩 백엔드 자동 감지 (LLM API 없이도 동작)
# frozen 번들이면 동봉 모델 폴더 우선(오프라인). 그 외엔 HF 식별자(개발).
_DEFAULT_MODEL = str(paths.MODEL_DIR) if paths.MODEL_DIR.exists() else "jhgan/ko-sroberta-multitask"
EMBED_MODEL_PATH = os.getenv("EMBED_MODEL_PATH", _DEFAULT_MODEL)
USE_LOCAL_EMBED = False
USE_BM25 = os.getenv("USE_BM25", "true").lower() == "true"
USE_API = bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY"))

_embed_model = None
try:
    if not USE_API:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(EMBED_MODEL_PATH)
        USE_LOCAL_EMBED = True
except Exception as _e:
    print(f"[warn] sentence-transformers 로드 실패 → bigram fallback 사용: {_e}")

try:
    from rank_bm25 import BM25Okapi
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False
    USE_BM25 = False

# Ollama LLM 보정 (선택적 — 정확도 향상용)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# OpenAI 옵트인 — .env 키 존재 시 우선 사용(없으면 로컬 Ollama). 키는 헤더로만, 마스킹.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or None
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
USE_OLLAMA = os.getenv("USE_OLLAMA", "auto").lower()  # auto / true / false
_OLLAMA_AVAILABLE = False


# ----------------------------------------------------------------------------
# FastAPI 앱
# ----------------------------------------------------------------------------
async def check_ollama() -> bool:
    """Ollama 서버 가용성 확인 + 모델 존재 여부 검증."""
    global _OLLAMA_AVAILABLE
    if USE_OLLAMA == "false":
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code != 200:
                return False
            models = [m["name"] for m in r.json().get("models", [])]
            if not any(OLLAMA_MODEL.split(":")[0] in m for m in models):
                print(f"[warn] Ollama 모델 미발견: {OLLAMA_MODEL}. 'ollama pull {OLLAMA_MODEL}' 실행 필요")
                return False
            _OLLAMA_AVAILABLE = True
            return True
    except Exception as e:
        if USE_OLLAMA == "true":
            print(f"[warn] Ollama 강제 활성화 설정이지만 연결 실패: {e}")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] library: {LIBRARY_ROOT.resolve()}")
    print(f"[startup] embedding backend: " + (
        f"local model ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "API" if USE_API
        else "bigram fallback (저정확도)"
    ))
    print(f"[startup] BM25 하이브리드: {USE_BM25 and _HAS_BM25}")
    ollama_ok = await check_ollama()
    print(f"[startup] Ollama LLM 보정: {'ON (' + OLLAMA_MODEL + ')' if ollama_ok else 'OFF'}")
    yield


app = FastAPI(title="주석 비교 에이전트 v2 (라이브러리)", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



# ----------------------------------------------------------------------------
# 유틸 — 경로·검증·카탈로그
# ----------------------------------------------------------------------------
def validate_period(period: str) -> str:
    if not PERIOD_PATTERN.match(period):
        raise HTTPException(400, f"기간 형식 오류: {period} (예: 2025Q3, 2024FY)")
    return period


# 기간 정렬키 — 연도 + 분기순(Q1<Q2<Q3<FY). FY(사업보고서)는 연말이라 Q3 뒤.
_PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}


def period_sort_key(period: str):
    try:
        return (int(period[:4]), _PERIOD_ORDER.get(period[4:], 9))
    except (ValueError, IndexError):
        return (9999, 9)


def validate_company(company: str) -> str:
    if company not in VALID_COMPANIES:
        raise HTTPException(400, f"알 수 없는 회사: {company}")
    return company


def entry_dir(company: str, period: str) -> Path:
    return LIBRARY_ROOT / validate_company(company) / validate_period(period)


# ── doc_type 3문서 모델 ──────────────────────────────────────────────────────
# 한 (회사,기간) 셀이 report(분기보고서)·review(연결검토보고서)·review_sep(별도검토보고서)
# 세 문서를 보유. 모든 신규 파라미터 기본 "report" → 기존 호출·셀 동작 100% 하위호환.
VALID_DOC_TYPES = {"report", "review", "review_sep"}


def validate_doc_type(doc_type: Optional[str]) -> str:
    """doc_type 검증 — 미지정(None/빈값)이면 기존 동작 보존 위해 "report" 기본.

    허용 목록(whitelist) 방식. 그 외 값은 400 으로 즉시 차단.
    """
    if not doc_type:
        return "report"
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(400, f"알 수 없는 문서유형: {doc_type} (report|review|review_sep)")
    return doc_type


def pdf_path(company: str, period: str, doc_type: str = "report") -> Path:
    """문서유형별 작업본 PDF 경로. report→report.pdf, review→review.pdf, review_sep→review_sep.pdf."""
    dt = validate_doc_type(doc_type)
    # 3분기 명시 분기 — report/review 파일명 문자열은 기존과 불변(하위호환).
    if dt == "report":
        filename = "report.pdf"
    elif dt == "review":
        filename = "review.pdf"
    else:  # review_sep
        filename = "review_sep.pdf"
    return entry_dir(company, period) / filename


def index_path(company: str, period: str, doc_type: str = "report") -> Path:
    """문서유형별 인덱스 경로. report→index.json, review→index_review.json, review_sep→index_review_sep.json."""
    dt = validate_doc_type(doc_type)
    # 3분기 명시 분기 — report/review 인덱스 파일명은 기존과 불변(하위호환).
    if dt == "report":
        filename = "index.json"
    elif dt == "review":
        filename = "index_review.json"
    else:  # review_sep
        filename = "index_review_sep.json"
    return entry_dir(company, period) / filename


# 표준 작업본 파일명 — 원본명 폴백 스캔에서 반드시 제외(표준본을 "원본"으로 오인 방지).
_STANDARD_PDF_NAMES = {"report.pdf", "review.pdf", "review_sep.pdf"}


def _filename_matches_doc_type(name: str, doc_type: str) -> bool:
    """bracketed 원본 PDF 파일명이 doc_type 에 해당하는지 키워드로 판정.

    - report     : 분기/반기/사업보고서 포함 & 검토·감사 미포함(본문 보고서).
    - review      : 연결검토 포함(연결재무제표 검토보고서).
    - review_sep  : 검토 포함 & 연결 미포함(별도재무제표 검토보고서).
    """
    if doc_type == "report":
        return (("분기" in name or "반기" in name or "사업보고서" in name)
                and "검토" not in name and "감사" not in name)
    if doc_type == "review":
        return "연결검토" in name
    if doc_type == "review_sep":
        return "검토" in name and "연결" not in name
    return False


def original_pdf_name(company: str, period: str, doc_type: str) -> Optional[str]:
    """문서유형별 '원본' PDF 파일명을 추정. 없으면 None.

    ① meta.json 의 documents[] 에서 doc_type 매칭 항목의 filename_original 우선.
    ② 없으면 entry_dir 내 *.pdf 스캔 폴백(키워드 매칭). 표준 작업본(report.pdf 등)은 제외.
    표시·다운로드용 원본명 노출 목적 — 작업본 파일명(report.pdf)을 그대로 노출하지 않기 위함.
    """
    dt = validate_doc_type(doc_type)
    d = entry_dir(company, period)

    # ① meta.json documents[] 우선
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for doc in meta.get("documents", []):
                fn = doc.get("filename_original")
                # 표준 작업본명(report.pdf 등)이 meta 에 잘못 들어간 경우 방어 — 폴백 스캔으로.
                if doc.get("doc_type") == dt and fn and fn.lower() not in _STANDARD_PDF_NAMES:
                    return fn
        except (OSError, json.JSONDecodeError):
            pass  # meta 손상/접근 불가 → 폴백 스캔으로 진행

    # ② 디렉터리 *.pdf 스캔 폴백 — 표준 작업본 3종 제외 후 키워드 매칭.
    try:
        for pdf in sorted(d.glob("*.pdf")):
            if pdf.name.lower() in _STANDARD_PDF_NAMES:
                continue
            if _filename_matches_doc_type(pdf.name, dt):
                return pdf.name
    except OSError:
        pass  # 디렉터리 접근 불가 → None

    return None


def load_catalog() -> dict:
    if not CATALOG_PATH.exists():
        return {"updated_at": None, "entries": []}
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def save_catalog(catalog: dict):
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upsert_catalog_entry(company: str, period: str, **fields):
    cat = load_catalog()
    cat["entries"] = [
        e for e in cat["entries"]
        if not (e["company"] == company and e["period"] == period)
    ]
    entry = {"company": company, "period": period, **fields}
    cat["entries"].append(entry)
    save_catalog(cat)


def remove_catalog_entry(company: str, period: str):
    cat = load_catalog()
    cat["entries"] = [
        e for e in cat["entries"]
        if not (e["company"] == company and e["period"] == period)
    ]
    save_catalog(cat)


# ----------------------------------------------------------------------------
# 임베딩·토큰화
# ----------------------------------------------------------------------------
def _bigram_embedding(text: str) -> np.ndarray:
    """최후의 fallback — 모델·API 모두 없을 때만 사용 (저정확도)."""
    text = (text or "").lower()
    vec = np.zeros(512, dtype=np.float32)
    for i in range(len(text) - 1):
        bigram = text[i:i + 2]
        idx = hash(bigram) % 512
        vec[idx] += 1.0
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _local_embedding_sync(text: str) -> np.ndarray:
    return _embed_model.encode(text, normalize_embeddings=True).astype(np.float32)


async def make_embedding(text: str) -> np.ndarray:
    """임베딩 생성 — 우선순위: 로컬 모델 > API > bigram fallback."""
    if USE_LOCAL_EMBED:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _local_embedding_sync, text)
    if USE_API:
        # 실제 운영: from openai import AsyncOpenAI; ...
        return _bigram_embedding(text)
    return _bigram_embedding(text)


_KOREAN_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)

# Phase B: 형태소 토크나이저(kiwipiepy). 내용형태소만 유지(조사·어미·문장부호 제거).
_KIWI = None
_KIWI_TRIED = False
# 명사(NNG/NNP)·외국어(SL)·한자(SH)·숫자(SN)·동사/형용사 어간(VV/VA)·어근(XR)
_KIWI_KEEP = {"NNG", "NNP", "SL", "SH", "SN", "VV", "VA", "XR"}


# A-2(kiwi 사용자사전)·B-3(주기 청킹) 실험은 2026-06-04 실측에서 교차변형 재현율 회귀
# (골든 1.0→0.88, 동의어-교차 1.0→0.571)로 롤백함 — 세밀 형태소 분할이 이 코퍼스의
# 교차변형 BM25 중첩(공유 sub-토큰)에 더 유리. 개발노트 참조.
def _get_kiwi():
    """kiwipiepy 싱글톤(최초 1회 로드, 폐쇄망 오프라인). 미설치/실패 시 None → 정규식 폴백."""
    global _KIWI, _KIWI_TRIED
    if _KIWI_TRIED:
        return _KIWI
    _KIWI_TRIED = True
    try:
        from kiwipiepy import Kiwi
        _KIWI = Kiwi()
    except Exception as e:
        print(f"[warn] kiwipiepy 로드 실패 → 정규식 토크나이저 폴백: {e}")
        _KIWI = None
    return _KIWI


def tokenize_korean(text: str) -> list:
    """형태소 토크나이저 — kiwipiepy 설치 시 내용형태소 추출(조사 분리로 BM25 정밀↑),
    미설치 시 정규식 폴백. 인덱싱·질의 양쪽에서 동일 사용(코퍼스 일관)."""
    if not text:
        return []
    kiwi = _get_kiwi()
    if kiwi is not None:
        try:
            return [t.form for t in kiwi.tokenize(text)
                    if t.tag in _KIWI_KEEP and len(t.form) > 1]
        except Exception:
            pass  # 런타임 실패 시 정규식 폴백
    return [t for t in _KOREAN_TOKEN_RE.findall(text) if len(t) > 1]


# ----------------------------------------------------------------------------
# 1) Library — Upload / List / Get / Delete
# ----------------------------------------------------------------------------
def safe_original_filename(name: Optional[str]) -> Optional[str]:
    """업로드 원본 파일명을 디스크 저장용으로 정규화.

    file.filename 은 외부 입력 → 디렉터리 성분 제거(경로 트래버설 차단) +
    Windows 금지문자 치환. report.pdf(작업본)와 충돌 방지. None/빈값이면 None.
    """
    if not name:
        return None
    base = os.path.basename(name.replace("\\", "/"))      # 경로 성분 제거 → 파일명만
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(". ")
    if not base:
        return None
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    if base.lower() in ("report.pdf", "review.pdf"):        # 작업본(report/review)과 충돌 방지
        base = "original_" + base
    return base


@app.post("/api/library/upload")
async def upload_to_library(
    file: UploadFile = File(...),
    company: str = Form(...),
    period: str = Form(...),
    doc_type: str = Form("report"),
):
    """단일 (회사, 기간) PDF를 라이브러리에 업로드. doc_type 으로 report/review 구분."""
    dt = validate_doc_type(doc_type)
    target_dir = entry_dir(company, period)
    target_dir.mkdir(parents=True, exist_ok=True)

    target_pdf = pdf_path(company, period, dt)
    overwriting = target_pdf.exists()
    content = await file.read()
    target_pdf.write_bytes(content)

    # 원본 파일명으로도 같은 폴더에 1부 보존(표시·다운로드용). 작업본은 report.pdf/review.pdf 유지.
    original_stored = safe_original_filename(file.filename)
    if original_stored:
        (target_dir / original_stored).write_bytes(content)

    with fitz.open(target_pdf) as doc:
        page_count = doc.page_count

    # 카탈로그 1행/(회사,기간) 유지. review 업로드가 기존 report 행을 지우지 않도록 머지.
    existing = next((e for e in load_catalog()["entries"]
                     if e["company"] == company and e["period"] == period), {})
    existing = {k: v for k, v in existing.items() if k not in ("company", "period")}

    if dt == "review":
        # review 업로드 → review_* 필드만 갱신, report 필드 보존.
        fields = {
            **existing,
            "review_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "review_filename_original": file.filename,
            "review_pages": page_count,
            "review_size_mb": round(len(content) / (1024 * 1024), 2),
            "review_indexed": False,
            "review_notes_count": 0,
            "review_detected_unit": None,
        }
    elif dt == "review_sep":
        # review_sep 업로드 → review_sep_* 필드만 갱신, report/review 필드 보존(review 분기 미러링).
        fields = {
            **existing,
            "review_sep_uploaded_at": datetime.now(timezone.utc).isoformat(),
            "review_sep_filename_original": file.filename,
            "review_sep_pages": page_count,
            "review_sep_size_mb": round(len(content) / (1024 * 1024), 2),
            "review_sep_indexed": False,
            "review_sep_notes_count": 0,
            "review_sep_detected_unit": None,
        }
    else:
        # report 업로드 → 기존 동작 그대로(report 필드 갱신), review_* 보존.
        fields = {
            **existing,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "filename_original": file.filename,
            "original_file": original_stored,
            "pages": page_count,
            "size_mb": round(len(content) / (1024 * 1024), 2),
            "indexed": False,
            "notes_count": 0,
            "detected_unit": None,
        }
    upsert_catalog_entry(company, period, **fields)

    return {
        "company": company,
        "period": period,
        "doc_type": dt,
        "pages": page_count,
        "indexed": False,
        "warning": "기존 항목을 덮어썼습니다." if overwriting else None,
    }


@app.get("/api/library")
async def get_library():
    """매트릭스 형태로 라이브러리 카탈로그 반환."""
    cat = load_catalog()
    matrix: Dict[str, List[str]] = {c: [] for c in VALID_COMPANIES}
    periods_seen: set = set()
    indexed_count = 0

    for e in cat["entries"]:
        matrix.setdefault(e["company"], []).append(e["period"])
        periods_seen.add(e["period"])
        if e.get("indexed"):
            indexed_count += 1

    for c in matrix:
        matrix[c] = sorted(set(matrix[c]), key=period_sort_key)

    # 각 엔트리에 문서유형별 원본 PDF 파일명 노출(PDF 뷰어 표시·다운로드용). 없으면 None.
    # 카탈로그 원본은 변형하지 않고 표시용 사본에만 주입(부수효과 차단).
    enriched = []
    for e in cat["entries"]:
        company, period = e["company"], e["period"]
        enriched.append({
            **e,
            "report_filename_original": original_pdf_name(company, period, "report"),
            "review_filename_original": original_pdf_name(company, period, "review"),
            "review_sep_filename_original": original_pdf_name(company, period, "review_sep"),
        })

    return {
        "matrix": matrix,
        "available_periods": sorted(periods_seen, key=period_sort_key),
        "total_files": len(cat["entries"]),
        "total_indexed": indexed_count,
        "entries": enriched,
    }


# 주의: 파라미터 라우트(/{company}/{period})보다 먼저 선언해야 함.
# FastAPI 는 선언 순서로 매칭하므로, 뒤에 두면 index/status 가 company=index 로 잘못 매칭됨.
@app.get("/api/library/index/status")
async def get_index_status():
    """전체 라이브러리의 인덱싱 상태."""
    return {"items": [{"key": k, **v} for k, v in INDEX_STATUS.items()]}


# 주의: 아래 정적 경로는 반드시 "/{company}/{period}" 파라미터 라우트보다 먼저
# 선언해야 한다(안 그러면 company="collect"/period="status" 로 오매칭).
@app.get("/api/library/collect/status")
async def get_collect_status():
    """전체 라이브러리의 DART 수집 상태."""
    return {"items": [{"key": k, **v} for k, v in COLLECT_STATUS.items()]}


@app.get("/api/library/{company}/{period}")
async def get_library_entry(company: str, period: str):
    validate_company(company)
    validate_period(period)
    cat = load_catalog()
    entry = next(
        (e for e in cat["entries"]
         if e["company"] == company and e["period"] == period),
        None,
    )
    if not entry:
        raise HTTPException(404, "항목을 찾을 수 없습니다.")
    return entry


@app.delete("/api/library/{company}/{period}")
async def delete_library_entry(company: str, period: str):
    d = entry_dir(company, period)
    if d.exists():
        # 하위 디렉터리(collect_dart 의 xbrl/·source/)도 있을 수 있어 rmtree 로 삭제.
        shutil.rmtree(d)
    remove_catalog_entry(company, period)
    # INDEX_STATUS 키가 "{company}/{period}/{doc_type}" 이므로 두 문서유형 모두 정리.
    for dt in VALID_DOC_TYPES:
        INDEX_STATUS.pop(f"{company}/{period}/{dt}", None)
    return {"deleted": True, "company": company, "period": period}


# ----------------------------------------------------------------------------
# 2) Indexing — 주석 구조 추출 + 임베딩
# ----------------------------------------------------------------------------
NOTE_HEADER_PATTERN = re.compile(
    r"^\s*(\d{1,2})\.\s+([가-힣A-Za-z][^\n]{2,60})",
    re.MULTILINE,
)
NOTES_SECTION_KEYWORDS = ["주석", "Notes to", "재무제표에 대한 주석"]
UNIT_PATTERN = re.compile(r"(?:단위[:\s]*)?(백만원|억원|천원|원|KRW)")

# 주석 시퀀스 탐지 상수
# - GAP_TOLERANCE: 헤더 일부가 추출되지 않아 번호가 건너뛰어도 같은 시퀀스로 인정할 최대 간격
#   (예: 신한 14→16 처럼 표/페이지 레이아웃 때문에 일부 헤더가 누락되는 경우 흡수)
# - MIN_VALID_MAX_NO: 진짜 주석 시퀀스로 인정할 도달 최대 번호 하한.
#   재무제표 표의 번호행 런은 max_no 가 작아(<15) 자연히 탈락.
GAP_TOLERANCE = 4
MIN_VALID_MAX_NO = 15

# 제목 끝의 주석참조 꼬리 (예: "...관련 손익(주석12)") 제거용
_NOTE_REF_TAIL = re.compile(r"\s*\(주석\s*\d+\)\s*$")

# ── Step 5: 문서유형 인지 추출 ──────────────────────────────────────────────
# 전체 분기/사업보고서는 진짜 주석 제목에 (연결)/(별도) 접미가 일관 존재한다.
# 슬림 검토보고서는 접미가 전혀 없다(문서 전체가 연결 주석).
#   _FS_DIV_SUFFIX: 제목 끝의 연결/별도 접미 (그룹 분리·fs_div 태깅·제목에서 제거).
#   _TOC_LEADER:    목차 점선 leader(`...` 3개↑ 또는 `…`) — 후보·제목에서 제거.
#   FULL_REPORT_SUFFIX_MIN: 이 수 이상 접미 헤더가 있으면 full_report 모드로 판정.
_FS_DIV_SUFFIX = re.compile(r"\s*\((연결|별도)\)\s*$")
_TOC_LEADER = re.compile(r"\.{3,}|…")
FULL_REPORT_SUFFIX_MIN = 5
# full_report 접미 그룹 전용 gap 허용치. 접미로 이미 진짜 주석만 남았으므로(비주석 흡수 위험 없음)
# 슬림(4)보다 크게 잡아 표/페이지 레이아웃으로 일부 헤더가 누락된 구간을 흡수
# (예: 사업보고서 연결 주석 no22→no27 의 +5 단절). 슬림 경로는 영향 없음(기본값 유지).
FULL_REPORT_GAP_TOLERANCE = 7


def _collect_header_candidates(doc) -> List[Dict[str, Any]]:
    """전체 페이지에서 `숫자. 제목` 형태 헤더 후보를 페이지 순서대로 수집.

    각 후보에 fs_div(연결/별도/None) 태그를 부착한다(접미 인지). 목차(점선 leader)
    라인은 후보에서 제외한다 — 전체보고서 목차의 `N.제목......페이지` 가 흡수되는 것을 차단.
    """
    candidates: List[Dict[str, Any]] = []
    for pno in range(doc.page_count):
        text = doc[pno].get_text()
        for m in NOTE_HEADER_PATTERN.finditer(text):
            note_no = int(m.group(1))
            title = m.group(2).strip()
            # 제목이 너무 짧거나 숫자로 시작하면(표 셀 잔재) 제외
            if len(title) < 3 or re.match(r"^\d", title):
                continue
            # 목차 라인(점선 leader 포함) 제외
            if _TOC_LEADER.search(title):
                continue
            sm = _FS_DIV_SUFFIX.search(title)
            fs_div = sm.group(1) if sm else None
            candidates.append({"page": pno + 1, "no": note_no,
                               "title": title, "fs_div": fs_div})
    return candidates


def _simulate_monotonic_run(candidates: List[Dict[str, Any]], start_idx: int,
                            gap_tolerance: int = GAP_TOLERANCE):
    """start_idx 후보에서 출발해 엄격 증가(gap 허용) 런을 시뮬레이션.

    채택 규칙: 다음 후보 번호가 직전 채택 번호보다 크고 (직전 + gap_tolerance) 이하면 채택.
    번호가 같거나 작으면(하위표 리셋) 건너뜀.

    gap_tolerance: 슬림은 기본값(GAP_TOLERANCE). full_report 접미 그룹은 후보 전체가
    이미 (연결)/(별도) 접미로 검증된 진짜 주석이라 더 큰 값을 허용(헤더 일부 미추출 흡수).

    Returns:
        (멤버 인덱스 리스트, 도달 최대 번호, 걸친 distinct 페이지 수)
    """
    last_no = candidates[start_idx]["no"]
    members = [start_idx]
    for j in range(start_idx + 1, len(candidates)):
        no = candidates[j]["no"]
        if last_no < no <= last_no + gap_tolerance:
            members.append(j)
            last_no = no
    pages = {candidates[k]["page"] for k in members}
    return members, last_no, len(pages)


def _select_note_sequence(candidates: List[Dict[str, Any]],
                          gap_tolerance: int = GAP_TOLERANCE):
    """no==1 시작 후보들 중 도달 최대 번호가 가장 큰 단조 런을 진짜 주석 시퀀스로 선택.

    동률 시 페이지 span 이 큰 쪽 우선. 유효 시퀀스가 없으면(max_no < MIN_VALID_MAX_NO) None 반환.
    """
    start_indices = [i for i, c in enumerate(candidates) if c["no"] == 1]
    best = None  # (max_no, page_span, members)
    for si in start_indices:
        members, max_no, page_span = _simulate_monotonic_run(candidates, si, gap_tolerance)
        key = (max_no, page_span)
        if best is None or key > (best[0], best[1]):
            best = (max_no, page_span, members)

    if best is None or best[0] < MIN_VALID_MAX_NO:
        return None
    return [candidates[k] for k in best[2]]


def _clean_title(title: str) -> str:
    """제목 정리 — 끝의 ` :`/`:`, `(주석N)` 참조 꼬리, `(연결)`/`(별도)` 접미,
    목차 점선 leader 꼬리 제거. 번역·요약·치환 없음(원문 발췌)."""
    title = _TOC_LEADER.split(title)[0]      # 점선 leader 이후(목차 페이지번호 등) 제거
    title = _FS_DIV_SUFFIX.sub("", title)    # 연결/별도 접미 제거(fs_div 로 별도 보존)
    title = _NOTE_REF_TAIL.sub("", title)
    title = title.rstrip()
    title = re.sub(r"\s*:\s*$", "", title)  # KB 처럼 헤더 끝에 붙는 콜론 제거
    return title.strip()


def _detect_unit(doc, scan_start_page: int) -> Optional[str]:
    """주석 시작 페이지부터 단위 표기를 카운트해 최빈값 반환. 환산 없이 탐지만."""
    unit_counter: Dict[str, int] = {}
    start = max(scan_start_page - 1, 0)  # page 번호(1-base) → 인덱스(0-base)
    for pno in range(start, doc.page_count):
        for m in UNIT_PATTERN.finditer(doc[pno].get_text()):
            unit_counter[m.group(1)] = unit_counter.get(m.group(1), 0) + 1
    return max(unit_counter, key=unit_counter.get) if unit_counter else None


def _build_notes_from_sequence(members: List[Dict[str, Any]], total_pages: int,
                               fs_div: Optional[str] = None):
    """선택된 시퀀스 멤버를 반환 형식의 notes 로 변환. page_end = 다음 주석 시작 - 1.

    fs_div: 그룹 전체에 강제할 연결/별도 값(full_report). None 이면 후보의 개별 fs_div 사용.
    """
    notes = []
    for i, c in enumerate(members):
        page_end = members[i + 1]["page"] - 1 if i + 1 < len(members) else total_pages
        notes.append({
            "no": c["no"],
            "title": _clean_title(c["title"]),
            "page_start": c["page"],
            "page_end": max(page_end, c["page"]),
            "fs_div": fs_div if fs_div is not None else c.get("fs_div"),
        })
    return notes


def _fallback_keyword_notes(doc):
    """비표준 문서 폴백 — 기존 방식(첫 '주석' 키워드 페이지부터 헤더 수집)."""
    notes_start_page = 0
    for pno in range(doc.page_count):
        if any(kw in doc[pno].get_text() for kw in NOTES_SECTION_KEYWORDS):
            notes_start_page = pno
            break

    candidates = []
    seen = set()
    for pno in range(notes_start_page, doc.page_count):
        for m in NOTE_HEADER_PATTERN.finditer(doc[pno].get_text()):
            note_no = int(m.group(1))
            title = m.group(2).strip()
            if len(title) < 3 or re.match(r"^\d", title):
                continue
            key = (note_no, pno + 1)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"page": pno + 1, "no": note_no, "title": title})

    return _build_notes_from_sequence(candidates, doc.page_count), notes_start_page + 1


# 별도(개별)재무제표 주석 섹션 헤더 — 분기보고서에서 연결 주석 뒤에 위치.
# "(별도)" 접미가 없는 회사가 있어(예: 신한·우리) 섹션 헤더로 별도 영역을 앵커링한다.
_SEPARATE_SECTION_RE = re.compile(r"(별도재무제표|재무제표에 대한 주석|재무제표\s*주석)")


def _find_separate_section_page(doc, after_page: int) -> Optional[int]:
    """연결 주석 영역(after_page) 이후 첫 '별도재무제표/재무제표 주석' 섹션 페이지(1-base).

    after_page 이후부터 스캔하므로 앞쪽 '연결재무제표 주석' 은 자연히 제외된다.
    """
    for pno in range(max(after_page, 0), doc.page_count):
        if _SEPARATE_SECTION_RE.search(doc[pno].get_text()):
            return pno + 1
    return None


def _extract_full_report_notes(candidates: List[Dict[str, Any]], total_pages: int, doc=None):
    """full_report 추출 — 연결은 (연결) 접미 앵커, 별도는 접미 또는 섹션 앵커.

    1) 연결: (연결) 접미 후보 그룹 → 단조 런.
    2) 별도: (별도) 접미 그룹이 있으면 그것으로, 없으면(접미 미사용 회사)
       연결 영역 이후 '재무제표 주석' 섹션 페이지부터의 무접미 후보로 단조 런(1.. 시작).
    각 그룹 fs_div 를 note 에 강제 태깅. 연결+별도 합쳐 반환.

    Returns: (notes, scan_start) — scan_start 는 단위 탐지 시작 페이지.
    """
    notes: List[Dict[str, Any]] = []
    scan_starts: List[int] = []

    # 1) 연결 (접미 앵커)
    conn_last_page = 0
    conn_group = [c for c in candidates if c.get("fs_div") == "연결"]
    if conn_group:
        seq = _select_note_sequence(conn_group, gap_tolerance=FULL_REPORT_GAP_TOLERANCE)
        if seq is not None:
            notes.extend(_build_notes_from_sequence(seq, total_pages, fs_div="연결"))
            scan_starts.append(seq[0]["page"])
            conn_last_page = max(c["page"] for c in seq)

    # 2) 별도 — 접미 그룹 우선, 없으면 섹션 앵커 폴백(접미 미사용 회사)
    sep_group = [c for c in candidates if c.get("fs_div") == "별도"]
    sep_seq = _select_note_sequence(sep_group, gap_tolerance=FULL_REPORT_GAP_TOLERANCE) if sep_group else None
    if sep_seq is None and doc is not None and conn_last_page:
        sep_start = _find_separate_section_page(doc, conn_last_page)
        if sep_start:
            sep_cands = [c for c in candidates
                         if c.get("fs_div") is None and c["page"] >= sep_start]
            sep_seq = _select_note_sequence(sep_cands, gap_tolerance=FULL_REPORT_GAP_TOLERANCE)
    if sep_seq is not None:
        notes.extend(_build_notes_from_sequence(sep_seq, total_pages, fs_div="별도"))
        scan_starts.append(sep_seq[0]["page"])

    scan_start = min(scan_starts) if scan_starts else 1
    return notes, scan_start


# Phase D: 노트 본문-존재 최소 글자수(이 미만이면 헤더 오탐으로 보고 드롭). 보수적 하한.
MIN_NOTE_BODY_CHARS = 30
# 본문/단위 스캔 시 노트당 페이지 상한(마지막 노트가 문서 끝까지 걸쳐도 폭주 방지).
NOTE_SCAN_PAGE_CAP = 8


def _note_body_text(doc, page_start: int, page_end: int, cap_pages: int = NOTE_SCAN_PAGE_CAP) -> str:
    """노트 페이지 범위의 본문 텍스트(캡 적용). 추출 검증·단위 탐지용(가공 없음)."""
    if not page_start:
        return ""
    last = min(page_start + cap_pages - 1, page_end or page_start, doc.page_count)
    out = []
    for p in range(page_start, last + 1):
        if 1 <= p <= doc.page_count:
            out.append(doc[p - 1].get_text())
    return "\n".join(out)


def _annotate_notes(doc, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """노트별 본문 존재 검증(헤더 오탐 드롭) + 노트별 단위 태깅. 텍스트 전용·환산 없음.

    - 제목 헤더만 잡히고 본문이 사실상 없는 후보(목차 잔재·표 셀 등) 제거.
    - 노트 페이지 범위에서 최빈 단위 표기를 note['detected_unit'] 로 보존(문서 1개 단위의 한계 보완).
    """
    kept = []
    for n in notes:
        body = _note_body_text(doc, n.get("page_start"), n.get("page_end"))
        body_chars = len(re.sub(r"\s", "", body))
        if body_chars < MIN_NOTE_BODY_CHARS:
            continue  # 본문 없는 헤더 오탐 — 드롭
        unit_counter: Dict[str, int] = {}
        for m in UNIT_PATTERN.finditer(body):
            unit_counter[m.group(1)] = unit_counter.get(m.group(1), 0) + 1
        n["detected_unit"] = max(unit_counter, key=unit_counter.get) if unit_counter else None
        kept.append(n)
    return kept


# Phase A: 본문 청크 파라미터. 인덱스 비대 방지 위해 노트당 청크·페이지 상한.
CHUNK_CHARS = 450          # 청크 목표 길이(자)
CHUNK_OVERLAP = 80         # 청크 간 겹침(경계 문맥 보존)
MAX_CHUNKS_PER_NOTE = 40   # 노트당 청크 상한(인덱스 용량 통제)
MAX_CHUNKS_PER_PAGE = 3    # 페이지당 청크 상한 — 긴 노트가 앞 페이지에서 예산을 소진하지 않고
                           # 전 페이지 범위에 고르게 분산되도록(긴 주석 커버리지↑)
CHUNK_SCAN_PAGE_CAP = 20   # 청크 스캔 페이지 상한
INDEX_SCHEMA = 2           # 청크 인덱싱 스키마 버전(구 인덱스=1/부재 → 제목-only 폴백)


def _chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """본문 텍스트를 overlap 슬라이딩 윈도우로 분할. 공백 정규화만(가공·요약 없음)."""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        return []
    chunks, i, n = [], 0, len(text)
    step = max(size - overlap, 1)
    while i < n:
        chunks.append(text[i:i + size])
        if i + size >= n:
            break
        i += step
    return chunks


def _note_chunks(doc, page_start: int, page_end: int) -> List[Dict[str, Any]]:
    """노트 페이지 범위를 페이지-정확 청크로 분할(각 청크에 실제 page 보존 → 인용 정밀화).

    반환: [{text, page}] (임베딩·토큰은 index_entry 에서 부착). 노트당 상한 적용.
    """
    if not page_start:
        return []
    out: List[Dict[str, Any]] = []
    last = min(page_start + CHUNK_SCAN_PAGE_CAP - 1, page_end or page_start, doc.page_count)
    for p in range(page_start, last + 1):
        if not (1 <= p <= doc.page_count):
            continue
        per_page = 0  # 페이지별 할당량 — 한 페이지가 노트 예산을 독식하지 못하게 분산
        for c in _chunk_text(doc[p - 1].get_text()):
            if len(c.strip()) < 20:  # 페이지 머리말·쪽번호 등 잔재 제외
                continue
            out.append({"text": c, "page": p})
            per_page += 1
            if len(out) >= MAX_CHUNKS_PER_NOTE:
                return out
            if per_page >= MAX_CHUNKS_PER_PAGE:
                break  # 이 페이지 할당량 소진 → 다음 페이지로(긴 노트 전 범위 커버)
    return out


def extract_notes_heuristic(pdf_path: Path, default_fs_div: str = "연결"):
    """PDF 에서 주석 헤더 시퀀스를 순수 휴리스틱(정규식+단조 런)으로 추출.

    문서유형 자동 판별:
    - (연결)/(별도) 접미 헤더가 임계(FULL_REPORT_SUFFIX_MIN) 이상 → full_report 모드
      (접미 앵커로 연결/별도 그룹 분리 추출). 그 외 → slim 모드(현행 단조런).

    default_fs_div: slim 모드에서 접미 미부착 노트에 태깅할 fs_div 기본값.
        검토보고서는 문서 단위로 연결/별도가 고정 → 호출부(index_entry)가 doc_type 으로 결정.
        review_sep 인덱싱 시 "별도", 그 외("연결") 기본 → report/review 동작 불변.
    AI/LLM 미사용. 반환 형식: (notes, detected_unit, source_type)
    - notes: [{"no","title","page_start","page_end","fs_div"}]
    - detected_unit: 탐지된 단위 표기(환산 없음) 또는 None
    - source_type: "full_report" | "slim"
    """
    doc = fitz.open(pdf_path)
    try:
        candidates = _collect_header_candidates(doc)
        suffix_count = sum(1 for c in candidates if c.get("fs_div"))

        if suffix_count >= FULL_REPORT_SUFFIX_MIN:
            # 전체 분기/사업보고서 — 접미 앵커로 연결/별도 분리 추출
            source_type = "full_report"
            notes, scan_start = _extract_full_report_notes(candidates, doc.page_count, doc)
        else:
            # 슬림 검토보고서 — 현행 단조런 유지
            source_type = "slim"
            sequence = _select_note_sequence(candidates)
            if sequence is not None:
                notes = _build_notes_from_sequence(sequence, doc.page_count)
                scan_start = sequence[0]["page"]
            else:
                # 비표준 문서 — 단조 시퀀스 탐지 실패 시 기존 키워드 방식으로 폴백
                notes, scan_start = _fallback_keyword_notes(doc)
            # 슬림 검토보고서는 문서 단위로 연결/별도 고정 → default_fs_div 로 태깅
            # (연결검토=review→"연결", 별도검토=review_sep→"별도"). 기본값 "연결" 유지 시 기존과 동일.
            for n in notes:
                if n.get("fs_div") is None:
                    n["fs_div"] = default_fs_div

        # Phase D: 본문-존재 검증(헤더 오탐 드롭) + 노트별 단위 태깅
        notes = _annotate_notes(doc, notes)
        detected_unit = _detect_unit(doc, scan_start)
        return notes, detected_unit, source_type
    finally:
        doc.close()


async def llm_refine_notes(candidates: list, sample_text: str) -> tuple:
    """Ollama LLM으로 1차 휴리스틱 결과 검증·보정. 실패 시 원본 그대로 반환.

    할루시네이션 방지 원칙:
    - 원본 텍스트에 없는 정보는 추가 금지
    - 확신 없으면 빈 응답 허용
    - JSON 스키마 강제, temperature=0
    """
    if not _OLLAMA_AVAILABLE:
        return candidates, "skipped"

    prompt = f"""당신은 한국 금융 감사보고서의 주석 구조를 검증하는 도구입니다.

엄격 규칙:
1. 제공된 원본 텍스트에서만 정보를 추출하세요. 외부 지식 사용 금지.
2. 원본에 명시되지 않은 정보는 절대 추가·추측하지 마세요.
3. 확신이 없는 항목은 결과에서 제외하세요. 추측은 오류보다 나쁩니다.
4. 숫자·금액·날짜는 생성하지 마세요. 주석 번호와 페이지만 허용.
5. 주석 제목은 원문 그대로 발췌하세요. 번역·요약·재구성 금지.

원본 텍스트 (일부):
---
{sample_text[:3000]}
---

휴리스틱으로 추출한 주석 후보:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

작업: 위 후보에서 명백히 잘못 추출된 항목(예: 표 데이터, 페이지 번호, 광고성 문구)을 제외하세요.
확신이 없는 항목도 제외하세요.

응답은 JSON 배열만 반환하세요. 다른 설명 텍스트 금지.
형식: [{{"no": 정수, "title": "제목", "page": 정수}}, ...]
"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 4000,
                        "top_p": 1.0,
                    },
                },
            )
            res.raise_for_status()
            text = res.json().get("response", "").strip()
            # JSON 배열 추출
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                return candidates, "no_json"
            refined = json.loads(m.group())
            # 검증: 원본 후보에 없는 제목은 제외 (할루시네이션 차단)
            original_titles = {c["title"] for c in candidates}
            verified = [r for r in refined
                        if isinstance(r, dict)
                        and "no" in r and "title" in r and "page" in r
                        and r["title"] in original_titles]
            if not verified:
                return candidates, "all_rejected"
            return verified, "ok"
    except Exception as e:
        print(f"[warn] Ollama 보정 실패: {e} → 휴리스틱 결과 사용")
        return candidates, f"error: {e}"


async def embed_and_write_index(company, period, doc_type, notes, detected_unit,
                                source_type, src_pdf, progress_cb=None) -> dict:
    """notes(확정) → 제목·청크 임베딩 + index.json(schema=2) 기록 + 카탈로그 upsert.
    progress_cb(frac:float) 있으면 노트별 진행률 보고(없으면 무시). INDEX_STATUS 미접근.
    반환: {"notes_count","n_conn","n_sep","detected_unit","total_pages"}."""
    dt = validate_doc_type(doc_type)

    # 제목 + 본문 청크 임베딩(Phase A). doc 1회 오픈으로 청크 본문 추출.
    with fitz.open(src_pdf) as doc:
        total_pages = doc.page_count
        for i, note in enumerate(notes):
            emb = await make_embedding(note["title"])
            note["embedding"] = emb.tolist()
            note["tokens"] = tokenize_korean(note["title"])
            # 본문 청크: 각 청크 임베딩+토큰(페이지 보존 → 인용 정밀)
            chunks = _note_chunks(doc, note.get("page_start"), note.get("page_end"))
            for ch in chunks:
                ce = await make_embedding(ch["text"])
                ch["embedding"] = ce.tolist()
                ch["tokens"] = tokenize_korean(ch["text"])
            note["chunks"] = chunks
            if progress_cb:
                progress_cb((i + 1) / max(len(notes), 1))

    # 연결/별도 분리 카운트 (full_report 에서 의미. slim 은 전부 연결)
    n_conn = sum(1 for n in notes if n.get("fs_div") == "연결")
    n_sep = sum(1 for n in notes if n.get("fs_div") == "별도")

    idx_path = index_path(company, period, dt)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump({
            "company": company,
            "period": period,
            "doc_type": dt,
            "schema": INDEX_SCHEMA,
            "total_pages": total_pages,
            "detected_unit": detected_unit,
            "source_type": source_type,
            "notes": notes,
        }, f, ensure_ascii=False)

    # 카탈로그 1행/(회사,기간) 유지 — 기존 행을 읽어 머지 후 upsert(통째 교체 방지).
    # company·period 는 위치 인자로 전달하므로 splat 대상에서 제외 (중복 인자 방지).
    existing = next((e for e in load_catalog()["entries"]
                     if e["company"] == company and e["period"] == period), {})
    existing = {k: v for k, v in existing.items() if k not in ("company", "period")}

    if dt == "review":
        # review 인덱싱 → review_* 접두 필드만 갱신, report 필드(indexed/notes_count 등) 보존.
        updates = {
            "review_indexed": True,
            "review_notes_count": len(notes),
            "review_notes_count_연결": n_conn,
            "review_notes_count_별도": n_sep,
            "review_source_type": source_type,
            "review_detected_unit": detected_unit,
            "review_indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    elif dt == "review_sep":
        # review_sep 인덱싱 → review_sep_* 접두 필드만 갱신, report/review 필드 보존.
        # 별도검토보고서는 연결 0/별도 N 이 정상(n_conn≈0). 별도 예외처리 없이 기존 카운트 경로 사용.
        updates = {
            "review_sep_indexed": True,
            "review_sep_notes_count": len(notes),
            "review_sep_notes_count_별도": n_sep,
            "review_sep_notes_count_연결": n_conn,
            "review_sep_source_type": source_type,
            "review_sep_detected_unit": detected_unit,
            "review_sep_indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        # report 인덱싱 → 기존 필드 그대로(하위호환), review_* 보존.
        updates = {
            "indexed": True,
            "notes_count": len(notes),
            "notes_count_연결": n_conn,
            "notes_count_별도": n_sep,
            "source_type": source_type,
            "detected_unit": detected_unit,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
    upsert_catalog_entry(company, period, **{**existing, **updates})

    return {
        "notes_count": len(notes),
        "n_conn": n_conn,
        "n_sep": n_sep,
        "detected_unit": detected_unit,
        "total_pages": total_pages,
    }


async def index_entry(company: str, period: str, doc_type: str = "report"):
    dt = validate_doc_type(doc_type)
    key = f"{company}/{period}/{dt}"
    src_pdf = pdf_path(company, period, dt)
    if not src_pdf.exists():
        INDEX_STATUS[key] = {"status": "error", "error": "PDF not found"}
        return

    INDEX_STATUS[key] = {"status": "running", "progress": 0.0, "stage": "extracting"}

    # review_sep(별도검토보고서)는 slim 노트를 별도로 태깅. report/review 는 "연결" 기본 → 인덱싱 결과 불변.
    default_fs_div = "별도" if dt == "review_sep" else "연결"
    notes, detected_unit, source_type = extract_notes_heuristic(src_pdf, default_fs_div=default_fs_div)
    INDEX_STATUS[key]["progress"] = 0.3

    # LLM 보정 단계 (선택적)
    if _OLLAMA_AVAILABLE and notes:
        INDEX_STATUS[key]["stage"] = "llm_refining"
        candidates = [{"no": n["no"], "title": n["title"], "page": n["page_start"]} for n in notes]
        sample_text = ""
        with fitz.open(src_pdf) as doc:
            for pno in range(min(notes[0]["page_start"] - 1 + 5, doc.page_count)):
                sample_text += doc[pno].get_text() + "\n"
                if len(sample_text) > 5000:
                    break
        refined, refine_status = await llm_refine_notes(candidates, sample_text)
        INDEX_STATUS[key]["llm_refine_status"] = refine_status

        # 보정된 후보를 원본 notes에 매핑하여 page_range 보존
        if refine_status == "ok":
            kept_titles = {r["title"] for r in refined}
            notes = [n for n in notes if n["title"] in kept_titles]
    else:
        INDEX_STATUS[key]["llm_refine_status"] = "skipped"

    INDEX_STATUS[key]["stage"] = "embedding"
    INDEX_STATUS[key]["progress"] = 0.5

    res = await embed_and_write_index(
        company, period, dt, notes, detected_unit, source_type, src_pdf,
        progress_cb=lambda f: INDEX_STATUS[key].update(progress=0.5 + 0.5 * f),
    )

    INDEX_STATUS[key] = {
        "status": "done",
        "progress": 1.0,
        "notes_extracted": res["notes_count"],
        "detected_unit": res["detected_unit"],
    }


@app.post("/api/library/index/{company}/{period}")
async def start_index(company: str, period: str, background: BackgroundTasks,
                      doc_type: str = "report"):
    # doc_type 은 쿼리 파라미터 — 경로 세그먼트 추가 금지(기존 라우트 충돌 방지).
    validate_company(company)
    validate_period(period)
    dt = validate_doc_type(doc_type)
    if not pdf_path(company, period, dt).exists():
        raise HTTPException(404, "PDF가 업로드되지 않았습니다.")
    # 동시 중복 인덱싱 가드 — 이미 진행 중이면 재시작 차단(상태 덮어쓰기/경합 방지)
    key = f"{company}/{period}/{dt}"
    if INDEX_STATUS.get(key, {}).get("status") == "running":
        raise HTTPException(409, "이미 인덱싱이 진행 중입니다.")
    background.add_task(index_entry, company, period, dt)
    return {"status": "running", "company": company, "period": period, "doc_type": dt}


# ----------------------------------------------------------------------------
# 2b) DART 자동 수집 — collect_dart 를 감싸 UI 버튼에서 호출
# ----------------------------------------------------------------------------
def _period_to_year_reprt(period: str):
    """라이브러리 period('2025Q3')→(year, reprt_code). Q4 등 DART 미지원 시 400."""
    year = int(period[:4])
    reprt = _SUFFIX_TO_REPRT.get(period[4:])
    if not reprt:
        raise HTTPException(400, f"DART 수집 미지원 기간: {period} (Q1/Q2/Q3/FY 만 지원)")
    return year, reprt


def _safe_err(e: Exception) -> str:
    """예외 메시지를 사용자 노출용으로 정제 — 마스킹은 safety 모듈로 중앙화."""
    return safety.safe_err(e)


def _collect_company_blocking(company: str, year: int, reprt: str,
                              period: str, want_report: bool) -> dict:
    """blocking DART 수집(httpx 동기) — asyncio.to_thread 로 실행.

    corp_code 해결(라이브 corpCode→실패 시 캐시/시드) 후 collect_company 호출.
    키는 .env 에서만 읽고 절대 반환/로그에 노출하지 않는다.
    """
    api_key = cdart.get_api_key()
    if not api_key:
        raise RuntimeError("DART_API_KEY 미설정(.env)")
    with httpx.Client(timeout=60.0) as client:
        try:
            corp_codes = cdart.fetch_and_store_corp_codes(client, api_key)
        except Exception:
            corp_codes = cdart.resolve_corp_codes(cdart._load_cached_corp_codes())
        corp_code = corp_codes.get(company) or cdart.SEED_CORP_CODES.get(company)
        if not corp_code:
            raise RuntimeError(f"{company} corp_code 미해결")
        odr = None
        if cdart._HAS_OPENDART:
            try:
                odr = cdart.OpenDartReader(api_key)
            except Exception:
                odr = None  # review/report 첨부만 스킵, 나머지 수집은 진행
        return cdart.collect_company(client, api_key, company, corp_code, year, reprt,
                                     period, odr=odr, collect_report=want_report)


async def _collect_and_index(company: str, period: str, want_report: bool):
    """백그라운드: DART 수집(스레드) → 확보된 표시용 PDF(doc_type) 자동 인덱싱."""
    key = f"{company}/{period}"
    COLLECT_STATUS[key] = {"status": "running", "stage": "collecting"}
    try:
        year, reprt = _period_to_year_reprt(period)
        meta = await asyncio.to_thread(_collect_company_blocking, company, year, reprt,
                                       period, want_report)
    except Exception as e:
        COLLECT_STATUS[key] = {"status": "error", "error": _safe_err(e)}
        return

    report_ok = bool(meta.get("report_collected"))
    review_ok = bool(meta.get("review_collected"))
    COLLECT_STATUS[key] = {"status": "indexing", "stage": "indexing",
                           "report_collected": report_ok, "review_collected": review_ok}
    indexed: List[str] = []
    try:
        if report_ok and pdf_path(company, period, "report").exists():
            await index_entry(company, period, "report")
            indexed.append("report")
        if review_ok and pdf_path(company, period, "review").exists():
            await index_entry(company, period, "review")
            indexed.append("review")
    except Exception as e:
        COLLECT_STATUS[key] = {"status": "error", "error": _safe_err(e),
                               "report_collected": report_ok, "review_collected": review_ok}
        return

    COLLECT_STATUS[key] = {
        "status": "done",
        "report_collected": report_ok,
        "review_collected": review_ok,
        "indexed": indexed,
        "rcept_no": meta.get("rcept_no"),
        "report_nm": meta.get("report_nm"),
    }


class CollectPayload(BaseModel):
    company: str
    period: str
    include_report_pdf: bool = True


@app.post("/api/library/collect")
async def start_collect(payload: CollectPayload, background: BackgroundTasks):
    company = validate_company(payload.company)
    period = validate_period(payload.period)
    _period_to_year_reprt(period)  # Q4 등 미지원 기간 조기 차단
    if not cdart.get_api_key():
        raise HTTPException(400, "DART_API_KEY 가 설정되지 않았습니다. backend/.env 에 키를 추가하세요.")
    key = f"{company}/{period}"
    if COLLECT_STATUS.get(key, {}).get("status") in ("running", "indexing"):
        raise HTTPException(409, "이미 수집이 진행 중입니다.")
    COLLECT_STATUS[key] = {"status": "running", "stage": "queued"}
    background.add_task(_collect_and_index, company, period, payload.include_report_pdf)
    return {"status": "running", "company": company, "period": period}


# ----------------------------------------------------------------------------
# 3) Compare — 비교 대상 명시 후 검색·매칭 (하이브리드)
# ----------------------------------------------------------------------------
class CompareTarget(BaseModel):
    company: str
    period: str
    # 미지정 시 "report" → 기존 호출(필드 생략) 동작·응답 불변.
    doc_type: Optional[Literal["report", "review", "review_sep"]] = "report"
    # 연결/별도 1급 차원. 동일 fs_div 끼리만 비교(연결↔연결, 별도↔별도). "all"=전체.
    fs_div: Optional[Literal["연결", "별도", "all"]] = "연결"


class ComparePayload(BaseModel):
    targets: List[CompareTarget] = Field(..., min_length=1, max_length=12)
    query: str
    mode: Literal["topic", "number"] = "topic"
    note_kind: Optional[Literal["전체", "주기", "서술형"]] = "전체"  # 주석 종류 필터


def cosine(a, b):
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def _notes_for_comparison(idx: dict, fs_div: str = "연결") -> list:
    """비교 대상 note 목록 — 동일 fs_div 끼리만 비교하도록 필터(연결/별도 1급 차원).

    - fs_div="all": 전체 note 반환(연결+별도).
    - fs_div="연결"|"별도": note 에 fs_div 태그가 있으면 해당 그룹만 반환.
      태그가 전혀 없는 구(舊) 인덱스는 전량 통과(하위호환). 슬림(검토보고서) 노트는
      전부 "연결" 태깅돼 있어 "별도" 요청 시 빈 목록 → 호출부에서 미지원으로 처리.
    """
    notes = idx.get("notes", [])
    if fs_div == "all":
        return notes
    has_fs_div = any(n.get("fs_div") for n in notes)
    if not has_fs_div:
        return notes
    return [n for n in notes if n.get("fs_div") == fs_div]


def _note_unit_cos(q_emb, note: dict):
    """노트 제목 + 본문청크 중 질의와 최고 cosine 및 그 page(인용 정밀). 구 인덱스는 제목만."""
    best = cosine(q_emb, note["embedding"]) if note.get("embedding") else -1.0
    page = note.get("page_start")
    for ch in note.get("chunks", []):
        e = ch.get("embedding")
        if not e:
            continue
        s = cosine(q_emb, e)
        if s > best:
            best, page = s, ch.get("page", page)
    return best, page


def _score_notes_for_query(q_emb, q_text: str, notes_with_emb: list) -> list:
    """전 노트의 final score·메타를 계산하는 공유 스코어링 코어(순수 규칙, AI 미사용).

    match_query_in_notes(argmax)·rank_notes_for_query(top-k) 가 동일 점수/임계/라벨을
    공유하도록 추출. 정렬·상위선택 정책은 호출부가 결정(여기선 노트 순서 보존).
    - Phase C: cosine·BM25 를 제목+본문청크로 확장. cosine=유닛 최댓값,
      BM25 코퍼스=제목+청크 토큰 합본. match_page=최고 유닛의 실제 페이지(인용 정밀).
    - lexical_hit: 질의 토큰이 제목 또는 청크 본문에 글자 그대로 존재하는가.
    - keep: 점수 플로어 통과 또는 (어휘 일치 + 완화 하한 통과).
    - confidence: 고점 + 어휘 일치 동시 충족 시에만 high, 그 외 low.

    Returns:
        notes_with_emb 와 동일 순서의 후보 dict 리스트(빈 입력이면 빈 리스트).
        각 dict: note_no/title/page_start/page_end/match_page/score(float)/
                 confidence/lexical_hit/keep/note_kind/min_match.
    """
    if not notes_with_emb:
        return []

    scored = [_note_unit_cos(q_emb, n) for n in notes_with_emb]
    cos_scores = np.array([s for s, _ in scored])
    match_pages = [p for _, p in scored]

    # Phase E: BM25·lexical 질의에 동의어 확장(임베딩 q_emb 는 원질의 유지 → 정밀도 보존).
    q_text_exp = synonyms.expand_query(q_text)

    if USE_BM25 and _HAS_BM25:
        corpus = []
        for n in notes_with_emb:
            toks = list(n.get("tokens") or tokenize_korean(n["title"]))
            for ch in n.get("chunks", []):
                toks.extend(ch.get("tokens") or [])
            corpus.append(toks or tokenize_korean(n["title"]))
        bm25 = BM25Okapi(corpus)
        bm_scores = bm25.get_scores(tokenize_korean(q_text_exp))
        bm_norm = bm_scores / max(bm_scores.max(), 1e-9)
        # B-1: note_kind 별 하이브리드 가중 — 주기(정책 서술)는 임베딩 비중↑, 서술형은 현행.
        kinds = [note_filters.note_kind(n.get("title", "")) for n in notes_with_emb]
        w_cos = np.array([COS_W_POLICY if k == "주기" else COS_W_DEFAULT for k in kinds])
        w_bm = np.array([BM_W_POLICY if k == "주기" else BM_W_DEFAULT for k in kinds])
        final = w_cos * cos_scores + w_bm * bm_norm
    else:
        final = cos_scores

    q_tokens = tokenize_korean(q_text_exp)
    out = []
    for i, n in enumerate(notes_with_emb):
        score = float(final[i])
        lexical_hit = any(qt in n["title"] for qt in q_tokens) or any(
            qt in (ch.get("text") or "") for ch in n.get("chunks", []) for qt in q_tokens)
        # B-2: 주기(정책 서술)는 키워드 빈약 → 채택 하한 완화. 서술형은 현행 MIN_MATCH_SCORE.
        note_kind = note_filters.note_kind(n.get("title", ""))
        min_match = POLICY_MIN_MATCH if note_kind == "주기" else MIN_MATCH_SCORE
        keep = (score >= min_match) or (lexical_hit and score >= LEXICAL_FLOOR)
        confidence = "high" if (score >= HIGH_CONF and lexical_hit) else "low"
        out.append({
            "note_no": n["no"],
            "title": n["title"],
            "page_start": n["page_start"],
            "page_end": n["page_end"],
            "match_page": match_pages[i],
            "score": score,
            "confidence": confidence,
            "lexical_hit": lexical_hit,
            "keep": keep,
            "note_kind": note_kind,   # 주기/서술형 — 가중·임계 분기 근거(투명성)
            "min_match": round(min_match, 3),
        })
    return out


def match_query_in_notes(q_emb, q_text: str, notes_with_emb: list) -> Optional[dict]:
    """질의(임베딩+원문)를 한 회사 주석목록에 매칭 — final score argmax 단일 노트.

    compare()/coverage·structure-diff·topic-map 이 의존(무변경). 점수/임계/라벨은
    _score_notes_for_query 공유. 동률은 argmax 관례대로 최저 인덱스(결정론).

    Args:
        q_emb: 질의 임베딩 (np.ndarray)
        q_text: 질의 원문 (BM25·lexical 용)
        notes_with_emb: "embedding" 키를 가진 note dict 리스트(chunks 선택적)
    Returns:
        최고점 후보 dict 또는 후보 없음(빈 목록) 시 None.
    """
    cands = _score_notes_for_query(q_emb, q_text, notes_with_emb)
    if not cands:
        return None
    # 기존 np.argmax 동률 정책(최저 인덱스) 보존 — 무회귀.
    best_idx = int(np.argmax([c["score"] for c in cands]))
    return cands[best_idx]


def rank_notes_for_query(q_emb, q_text: str, notes_with_emb: list, k: int = 5) -> list:
    """질의에 대한 상위 k 후보를 score 내림차순으로 반환(낮은 신뢰도·keep=False 포함).

    주석 비교조회(/api/compare topic) 가 회사별 후보 목록을 보여주기 위한 진입점.
    match_query_in_notes 와 동일 스코어링(_score_notes_for_query) 을 재사용해 일관 보장.
    정렬: score desc, 동점이면 note_no asc(결정론 — 모델·플랫폼 불문 동일 순서).

    Args:
        q_emb: 질의 임베딩
        q_text: 질의 원문
        notes_with_emb: "embedding" 키 보유 note dict 리스트
        k: 반환 상한(기본 5)
    Returns:
        후보 dict 리스트(≤k). score 는 round3. keep=False 후보도 숨기지 않음.
    """
    cands = _score_notes_for_query(q_emb, q_text, notes_with_emb)
    cands.sort(key=lambda c: (-c["score"], c["note_no"]))
    top = cands[:max(0, k)]
    for c in top:
        c["score"] = round(c["score"], 3)
    return top


@app.post("/api/compare")
async def compare(payload: ComparePayload):
    matches, missing = [], []
    units_seen = set()

    q_emb = await make_embedding(payload.query) if payload.mode == "topic" else None

    for target in payload.targets:
        # doc_type 별 인덱스 경로 선택(기본 report). 비교 로직 자체는 인덱스-불가지(무변경).
        idx_path = index_path(target.company, target.period, target.doc_type)
        if not idx_path.exists():
            missing.append({"company": target.company, "period": target.period,
                            "doc_type": target.doc_type, "reason": "not indexed"})
            continue

        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        detected_unit = idx.get("detected_unit")

        if payload.mode == "number":
            try:
                target_no = int(payload.query)
            except ValueError:
                raise HTTPException(400, "번호 모드에는 숫자만 입력 가능합니다.")
            hit = next((n for n in note_filters.filter_notes(_notes_for_comparison(idx, target.fs_div), payload.note_kind) if n["no"] == target_no), None)
            if hit is None:
                # 정확매칭 실패 = 진짜 미발견. UI 통일을 위해 candidates 빈 목록 대신 missing.
                missing.append({"company": target.company, "period": target.period,
                                "doc_type": target.doc_type, "reason": "no match"})
                continue
            # number 모드: 결정론 정확매칭을 단일 후보로 통일(AI 무경유·페이지 인용 유지).
            candidates = [{
                "note_no": hit["no"],
                "title": hit["title"],
                "page_start": hit["page_start"],
                "page_end": hit["page_end"],
                "match_page": hit["page_start"],
                "score": 1.0,
                "confidence": "exact",
                "lexical_hit": None,
                "keep": True,
            }]
        else:
            notes_with_emb = [n for n in note_filters.filter_notes(_notes_for_comparison(idx, target.fs_div), payload.note_kind) if "embedding" in n]
            if not notes_with_emb:
                # missing 은 오직 인덱스 없음/임베딩 없음. 임계 미달은 후보로 노출(숨기지 않음).
                missing.append({"company": target.company, "period": target.period,
                                "doc_type": target.doc_type, "reason": "no embeddings"})
                continue
            # top-5 후보(낮은 신뢰도 포함). below_threshold 라도 missing 으로 보내지 않음.
            candidates = rank_notes_for_query(q_emb, payload.query, notes_with_emb, k=5)

        matches.append({
            "company": target.company,
            "period": target.period,
            "doc_type": target.doc_type,
            "fs_div": target.fs_div,
            "detected_unit": detected_unit,
            "candidates": candidates,
        })
        if detected_unit:
            units_seen.add(detected_unit)

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "query": payload.query,
        "mode": payload.mode,
        "matches": matches,
        "missing": missing,
        "unit_warning": len(units_seen) > 1,
        "units_seen": sorted(units_seen),
        "embedding_backend": embedding_backend,
        "thresholds": {
            "min_match_score": MIN_MATCH_SCORE,
            "lexical_floor": LEXICAL_FLOOR,
            "high_conf": HIGH_CONF,
        },
    }


# ----------------------------------------------------------------------------
# 4) Insights — 신한 관점 인사이트 (A 커버리지 매트릭스 / B 구조 차집합)
#    순수 규칙·임베딩만 사용. 숫자 재구성·단위 환산 없음, 외부 호출 없음.
# ----------------------------------------------------------------------------
class CoveragePayload(BaseModel):
    targets: List[CompareTarget] = Field(..., min_length=1, max_length=12)
    topics: Optional[List[str]] = None  # 생략 시 DEFAULT_TOPICS
    note_kind: Optional[Literal["전체", "주기", "서술형"]] = "전체"


def _load_index(company: str, period: str, doc_type: str = "report") -> Optional[dict]:
    """저장된 인덱스 로드(doc_type 별 경로). 없으면 None — 부재 셀 안전 처리."""
    idx_path = index_path(company, period, doc_type)
    if not idx_path.exists():
        return None
    return json.loads(idx_path.read_text(encoding="utf-8"))


def _load_topic_dict_topics() -> Optional[List[str]]:
    """주제사전 자동초안(topic_dict.json)에서 토픽 라벨 리스트 로드. 없으면 None.

    Step 7: coverage 토픽 미지정 시 우선 소스. 파일이 없거나 비정상이면 None 을
    돌려 호출부가 DEFAULT_TOPICS 로 폴백하게 한다(기존 동작 보존).
    """
    if not TOPIC_DICT_PATH.exists():
        return None
    try:
        data = json.loads(TOPIC_DICT_PATH.read_text(encoding="utf-8"))
        raw = [t["topic"] for t in data.get("topics", []) if t.get("topic")]
        # 중복 라벨 방어 — coverage 매트릭스가 topic 문자열을 키로 쓰므로 유일화(순서보존).
        topics = list(dict.fromkeys(raw))
        return topics or None
    except Exception as e:
        print(f"[warn] topic_dict.json 로드 실패 → DEFAULT_TOPICS 폴백: {e}")
        return None


@app.post("/api/coverage")
async def coverage(payload: CoveragePayload):
    """A. 횡단 커버리지 매트릭스 — 주제 × 회사 → 전용/관련/미발견.

    각 주제를 1회 임베딩 후 각 회사 주석목록에 match_query_in_notes 적용.
    셀 분류: keep=False→none / confidence=="high"→dedicated / 그 외(keep·low)→related.
    """
    # 토픽 소스 결정: 명시 topics > topic_dict.json 자동초안 > DEFAULT_TOPICS 폴백.
    if payload.topics:
        topics = payload.topics
        topic_source = "explicit"
    else:
        auto_topics = _load_topic_dict_topics()
        if auto_topics:
            topics = auto_topics
            topic_source = "topic_dict"
        else:
            topics = DEFAULT_TOPICS
            topic_source = "default"
    units_seen = set()

    # 각 target 인덱스 1회 로드 (재임베딩 없음 — 주제 임베딩만 생성)
    loaded: Dict[str, dict] = {}
    key_fsdiv: Dict[str, str] = {}  # key→fs_div (연결/별도 1급 차원)
    target_keys: List[str] = []
    # 매트릭스 행 키 — report 는 기존 "{회사}/{기간}" 유지(하위호환), review 만
    # "{회사}/{기간}/review" 로 구분(동일 셀 report/review 동시 비교 시 충돌 방지).
    for t in payload.targets:
        key = f"{t.company}/{t.period}" if t.doc_type == "report" \
            else f"{t.company}/{t.period}/{t.doc_type}"
        target_keys.append(key)
        key_fsdiv[key] = t.fs_div
        idx = _load_index(t.company, t.period, t.doc_type)
        loaded[key] = idx
        if idx and idx.get("detected_unit"):
            units_seen.add(idx["detected_unit"])

    matrix: Dict[str, Dict[str, Any]] = {}
    for topic in topics:
        q_emb = await make_embedding(topic)
        row: Dict[str, Any] = {}
        for key in target_keys:
            idx = loaded[key]
            if not idx:
                row[key] = {"coverage_level": "none", "best_score": None,
                            "reason": "not indexed"}
                continue
            notes_with_emb = [n for n in note_filters.filter_notes(_notes_for_comparison(idx, key_fsdiv.get(key, "연결")), payload.note_kind) if "embedding" in n]
            m = match_query_in_notes(q_emb, topic, notes_with_emb)
            if m is None:
                row[key] = {"coverage_level": "none", "best_score": None,
                            "reason": "no embeddings"}
                continue
            if not m["keep"]:
                # 임계값 미달 — 미발견(단, 참고용 best_score 노출)
                row[key] = {"coverage_level": "none",
                            "best_score": round(m["score"], 3)}
            else:
                level = "dedicated" if m["confidence"] == "high" else "related"
                row[key] = {
                    "coverage_level": level,
                    "note_no": m["note_no"],
                    "title": m["title"],
                    "page_start": m["page_start"],
                    "page_end": m["page_end"],
                    "score": round(m["score"], 3),
                    "confidence": m["confidence"],
                    "lexical_hit": m["lexical_hit"],
                }
        matrix[topic] = row

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "topics": topics,
        "topic_source": topic_source,
        "targets": [{"company": t.company, "period": t.period} for t in payload.targets],
        "matrix": matrix,
        "unit_warning": len(units_seen) > 1,
        "units_seen": sorted(units_seen),
        "embedding_backend": embedding_backend,
    }


class StructureDiffPayload(BaseModel):
    base: CompareTarget = Field(default_factory=lambda: CompareTarget(company="신한", period="2026Q1"))
    peers: List[CompareTarget] = Field(..., min_length=1, max_length=12)


def _best_cosine(emb, notes_with_emb: list) -> tuple:
    """emb 에 대해 notes_with_emb 중 최고 코사인 note 와 점수 반환. (note, score) 또는 (None, 0.0)."""
    best_note, best_s = None, -1.0
    for n in notes_with_emb:
        s = cosine(emb, n["embedding"])
        if s > best_s:
            best_note, best_s = n, s
    return (best_note, best_s) if best_note is not None else (None, 0.0)


@app.post("/api/structure-diff")
async def structure_diff(payload: StructureDiffPayload):
    """B. 신한 vs 동종 주석 차집합 — 저장된 임베딩으로 의미 매칭(재임베딩 없음).

    note A↔B 대응 = cosine(embA, embB) >= GAP_SIM_THRESHOLD.
    - base_only: base 에 있으나 어느 peer 에도 대응 없는 항목.
    - peer_disclosed_base_missing: ≥1 peer 가 가졌으나 base 에 대응 없는 항목
      (disclosed_by, peer_count 포함, peer_count 내림차순).
    """
    base_idx = _load_index(payload.base.company, payload.base.period, payload.base.doc_type)
    if not base_idx:
        raise HTTPException(404, f"base 인덱스 없음: {payload.base.company}/{payload.base.period}")

    base_notes = [n for n in _notes_for_comparison(base_idx, payload.base.fs_div) if "embedding" in n]

    peer_data: List[Dict[str, Any]] = []  # {company, period, notes}
    for p in payload.peers:
        idx = _load_index(p.company, p.period, p.doc_type)
        if not idx:
            continue
        peer_data.append({
            "company": p.company, "period": p.period,
            "notes": [n for n in _notes_for_comparison(idx, p.fs_div) if "embedding" in n],
        })
    if not peer_data:
        raise HTTPException(404, "대응할 peer 인덱스가 하나도 없습니다.")

    thr = GAP_SIM_THRESHOLD

    # 1) base_only: base note 가 어느 peer 에도 대응 없는 항목
    base_only = []
    for bn in base_notes:
        matched_any = False
        for pd in peer_data:
            _, s = _best_cosine(bn["embedding"], pd["notes"])
            if s >= thr:
                matched_any = True
                break
        if not matched_any:
            base_only.append({
                "note_no": bn["no"], "title": bn["title"],
                "page_start": bn["page_start"], "page_end": bn["page_end"],
            })

    # 2) peer_disclosed_base_missing: peer note 가 base 에 대응 없음
    #    동일 항목(여러 peer 가 같은 주제)을 묶기 위해 peer note 의 base 최고 유사 note 로 그룹화하지 않고,
    #    각 peer note 를 그 "대표 제목" 기준으로 합산한다. 단순 PoC: 제목 정규화 키로 묶음.
    gap_map: Dict[str, Dict[str, Any]] = {}
    for pd in peer_data:
        for pn in pd["notes"]:
            _, s = _best_cosine(pn["embedding"], base_notes)
            if s >= thr:
                continue  # base 에 대응 있음 → 갭 아님
            key = re.sub(r"\s+", "", pn["title"])  # 공백 무시 제목 키
            if key not in gap_map:
                gap_map[key] = {
                    "title": pn["title"],
                    "disclosed_by": [],
                    "base_best_score": round(s, 3),
                    "examples": [],
                }
            entry = gap_map[key]
            company = pd["company"]
            if company not in entry["disclosed_by"]:
                entry["disclosed_by"].append(company)
            entry["examples"].append({
                "company": pd["company"], "period": pd["period"],
                "note_no": pn["no"], "title": pn["title"],
                "page_start": pn["page_start"], "page_end": pn["page_end"],
                "base_best_score": round(s, 3),
            })

    peer_disclosed = []
    for entry in gap_map.values():
        peer_disclosed.append({
            "title": entry["title"],
            "disclosed_by": entry["disclosed_by"],
            "peer_count": len(entry["disclosed_by"]),
            "examples": entry["examples"],
        })
    # peer_count 높은 순(공시 갭 신호 강도), 동률 시 제목순
    peer_disclosed.sort(key=lambda e: (-e["peer_count"], e["title"]))

    embedding_backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "base": {"company": payload.base.company, "period": payload.base.period},
        "peers": [{"company": p["company"], "period": p["period"]} for p in peer_data],
        "threshold": thr,
        "base_only": base_only,
        "peer_disclosed_base_missing": peer_disclosed,
        "embedding_backend": embedding_backend,
    }


# ----------------------------------------------------------------------------
# 5) PDF 스트리밍 — PDF.js가 직접 로드
# ----------------------------------------------------------------------------
@app.get("/api/pdf")
async def serve_pdf(company: str, period: str, doc_type: str = "report"):
    # doc_type 생략 시 report.pdf — 기존 URL·동작 불변.
    target_pdf = pdf_path(company, period, doc_type)
    if not target_pdf.exists():
        raise HTTPException(404, "PDF를 찾을 수 없습니다.")
    # inline: 새 창(팝업) 열람 시 다운로드 대신 브라우저 PDF 뷰어로 표시(#page 이동 지원).
    # 원본명이 있으면 filename*=UTF-8'' 로 부여(한글 파일명 안전). 없으면 기존과 동일(inline 만).
    orig = original_pdf_name(company, period, doc_type)
    if orig:
        disposition = f"inline; filename*=UTF-8''{quote(orig)}"
    else:
        disposition = "inline"
    return FileResponse(target_pdf, media_type="application/pdf",
                        headers={"Content-Disposition": disposition})


# ----------------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    cat = load_catalog()
    backend = (
        f"local-embedding ({EMBED_MODEL_PATH})" if USE_LOCAL_EMBED
        else "api" if USE_API
        else "bigram-fallback"
    )
    return {
        "service": "4대 금융지주 주석 비교 에이전트 PoC v2",
        "embedding_backend": backend,
        "bm25_enabled": USE_BM25 and _HAS_BM25,
        "ollama_enabled": _OLLAMA_AVAILABLE,
        "openai_enabled": bool(OPENAI_API_KEY),
        "llm_provider": "openai" if OPENAI_API_KEY else ("ollama" if _OLLAMA_AVAILABLE else "none"),
        "ollama_model": OLLAMA_MODEL if _OLLAMA_AVAILABLE else None,
        "library_size": len(cat["entries"]),
    }


# ----------------------------------------------------------------------------
# 재무제표 비교 — fs_compare.py(결정론) 래핑. 모든 파생값에 provenance 동봉.
# ----------------------------------------------------------------------------
@app.get("/api/fs/accounts")
async def fs_accounts(company: str, period: str, fs_div: str = "연결"):
    return {"rows": fs_compare.list_accounts(company, period, fs_div)}


@app.get("/api/fs/delta")
async def fs_delta(company: str, period: str, fs_div: str = "연결"):
    return fs_compare.delta(company, period, fs_div)


@app.get("/api/fs/consolidated-vs-separate")
async def fs_cons_vs_sep(company: str, period: str):
    return fs_compare.consolidated_vs_separate(company, period)


@app.get("/api/fs/benchmark")
async def fs_benchmark(period: str, account_id: str, fs_div: str = "연결"):
    return fs_compare.benchmark(period, account_id, fs_div)


@app.get("/api/fs/ratio")
async def fs_ratio(company: str, period: str, fs_div: str = "연결"):
    return fs_compare.ratio(company, period, fs_div)


@app.get("/api/fs/timeseries")
async def fs_timeseries(company: str, account_id: str, fs_div: str = "연결"):
    return fs_compare.timeseries(company, account_id, fs_div)


@app.get("/api/fs/timeseries-accounts")
async def fs_timeseries_accounts():
    return {"accounts": [{"account_id": a, "account_nm": n} for a, n in fs_compare.TIMESERIES_ACCOUNTS]}


@app.get("/api/fs/flags")
async def fs_flags(company: str, period: str, fs_div: str = "연결"):
    return fs_compare.flags(company, period, fs_div)


@app.get("/api/fs/consolidated-subtotals")
async def fs_cons_subtotals(company: str, period: str):
    return fs_compare.consolidated_subtotals(company, period)


@app.get("/api/notes/account-refs")
async def notes_account_refs(company: str, period: str, fs_div: str = "연결", top_k: int = 2,
                             doc_type: Literal["report", "review", "review_sep"] = "report"):
    """재무제표 핵심계정 ↔ 주석 정합 참조 — 계정명 임베딩 ↔ note title 임베딩 cosine 상위.
    숫자 자동일치 금지(후보 제시·확정은 사용자). AI 무경유(임베딩 결정론)."""
    idx = _load_index(company, period, doc_type)
    if not idx:
        raise HTTPException(404, f"인덱스 없음: {company}/{period}")
    notes = [n for n in _notes_for_comparison(idx, fs_div) if "embedding" in n]
    out = []
    for aid, nm in fs_compare.TIMESERIES_ACCOUNTS:
        try:
            emb = (await make_embedding(nm)).tolist()
        except Exception as e:
            raise HTTPException(500, _safe_err(e))
        scored = sorted(
            ({"note_no": n["no"], "title": n["title"], "page_start": n.get("page_start"),
              "page_end": n.get("page_end"), "score": round(cosine(emb, n["embedding"]), 4)}
             for n in notes), key=lambda x: x["score"], reverse=True)
        out.append({"account_id": aid, "account_nm": nm, "matches": scored[:top_k]})
    return {"company": company, "period": period, "fs_div": fs_div, "rows": out}


@app.get("/api/notes/topic-map")
async def notes_topic_map(period: str, fs_div: str = "연결",
                          companies: Optional[str] = None, note_kind: str = "전체",
                          doc_type: Literal["report", "review", "review_sep"] = "report"):
    """§5.2 표준 주제 매핑 — 4사 주석을 canonical topic으로 분류·정렬(임베딩, AI 무경유)."""
    want = [c for c in (companies or "").split(",") if c] or list(VALID_COMPANIES)
    topics = _load_topic_dict_topics() or DEFAULT_TOPICS
    # 토픽 라벨 임베딩(결정론)
    try:
        topic_embs = {t: (await make_embedding(t)).tolist() for t in topics}
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    company_notes: Dict[str, list] = {}
    for c in want:
        idx = _load_index(c, period, doc_type)
        if not idx:
            continue
        notes = note_filters.filter_notes(_notes_for_comparison(idx, fs_div), note_kind)
        company_notes[c] = [n for n in notes if "embedding" in n]
    result = note_topics.build_topic_map(topic_embs, company_notes)
    result.update({"period": period, "fs_div": fs_div, "topic_min_score": note_topics.TOPIC_MIN_SCORE})
    return result


@app.get("/api/notes/compare-memo")
async def notes_compare_memo(topic: str, period: str, fs_div: str = "연결",
                             companies: Optional[str] = None, per_company: int = 1,
                             doc_type: Literal["report", "review", "review_sep"] = "report"):
    """§5.3 비교 메모 초안(옵트인) — 주제에 대한 4사 주석 정책·가정 차이 AI 초안.
    인용 강제: 근거(sources) 없으면 초안 생성 안 함. Ollama off=초안 없이 출처만."""
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "주제가 비어 있습니다.")
    want = [c for c in (companies or "").split(",") if c] or list(VALID_COMPANIES)
    try:
        t_emb = (await make_embedding(topic)).tolist()
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    # 회사별 주제 최근접 주석 top-N 수집(인용 출처)
    sources = []
    for c in want:
        idx = _load_index(c, period, doc_type)
        if not idx:
            continue
        cell = [{"company": c, "period": period, "index": idx}]
        got = notes_rag.retrieve(
            t_emb, cell, fs_div=fs_div, top_k=per_company,
            query_text=topic, tokenize=tokenize_korean, expand=synonyms.expand_query,
            bm25_cls=(BM25Okapi if (USE_BM25 and _HAS_BM25) else None),
            cos_w=COS_W_DEFAULT, cos_w_policy=COS_W_POLICY)
        for s in got:
            # 청크 본문 우선(정밀); 구 인덱스(text 없음)는 페이지 추출 폴백
            if not s.get("text"):
                s["text"] = notes_rag.extract_note_text(c, period, s.get("page_start"), s.get("page_end"))
            sources.append(s)
    if not sources:
        return {"topic": topic, "period": period, "fs_div": fs_div,
                "sources": [], "memo": None, "mode": "no_evidence", "ollama": _OLLAMA_AVAILABLE}
    memo, mode = None, "retrieval_only"
    if _OLLAMA_AVAILABLE or OPENAI_API_KEY:
        try:
            memo = await notes_rag.answer_compare_ollama(topic, sources, OLLAMA_URL, OLLAMA_MODEL, openai_key=OPENAI_API_KEY)
            mode = "memo" if memo else "retrieval_only"
        except Exception as e:
            print(f"[compare-memo] 생성 실패(무시): {_safe_err(e)}", file=sys.stderr)
    src_out = [{k: s.get(k) for k in ("company", "period", "fs_div", "note_no",
                                      "title", "page_start", "page_end", "match_page", "score")} for s in sources]
    return {"topic": topic, "period": period, "fs_div": fs_div,
            "sources": src_out, "memo": memo, "mode": mode, "ollama": _OLLAMA_AVAILABLE}


# ----------------------------------------------------------------------------
# 주석 RAG (§5.4, 옵트인) — 정성 텍스트 전용. 숫자 무경유·출처 인용 강제·Ollama 옵트인.
# ----------------------------------------------------------------------------
@app.get("/api/notes/rag")
async def notes_rag_query(q: str, fs_div: str = "연결",
                          companies: Optional[str] = None,
                          period: Optional[str] = None, top_k: int = 5,
                          note_kind: str = "전체", generate: bool = True,
                          doc_type: Literal["report", "review", "review_sep"] = "report"):
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "질의가 비어 있습니다.")
    want_companies = set((companies or "").split(",")) - {""} or set(VALID_COMPANIES)

    # 인덱싱된 report 셀 수집(주석 소스). 숫자 아님 — 주석 텍스트만.
    cells = []
    for e in load_catalog()["entries"]:
        if e.get("company") in want_companies and e.get("indexed"):
            if period and e.get("period") != period:
                continue
            idx = _load_index(e["company"], e["period"], doc_type)
            if idx:
                cells.append({"company": e["company"], "period": e["period"], "index": idx})

    try:
        q_emb = (await make_embedding(q)).tolist()
        sources = notes_rag.retrieve(
            q_emb, cells, fs_div=fs_div, top_k=top_k, note_kind=note_kind,
            query_text=q, tokenize=tokenize_korean, expand=synonyms.expand_query,
            bm25_cls=(BM25Okapi if (USE_BM25 and _HAS_BM25) else None),
            cos_w=COS_W_DEFAULT, cos_w_policy=COS_W_POLICY)
        for s in sources:
            # 청크 본문 우선(정밀 인용); 구 인덱스(text 없음)는 페이지 추출 폴백
            if not s.get("text"):
                s["text"] = notes_rag.extract_note_text(
                    s["company"], s["period"], s.get("page_start"), s.get("page_end"))
    except Exception as e:
        raise HTTPException(500, _safe_err(e))

    # 인용 강제(불변): 근거(sources) 없으면 답변 생성 안 함. Ollama off 면 retrieval_only.
    if not sources:
        return {"query": q, "fs_div": fs_div, "sources": [], "answer": None,
                "mode": "no_evidence", "ollama": _OLLAMA_AVAILABLE}
    answer = None
    mode = "retrieval_only"
    # generate=false: LLM 답변 생략(검색만) — 평가/디버깅용 고속 경로. 인용 강제는 유지.
    if generate and (_OLLAMA_AVAILABLE or OPENAI_API_KEY):
        try:
            answer = await notes_rag.answer_ollama(q, sources, OLLAMA_URL, OLLAMA_MODEL, openai_key=OPENAI_API_KEY)
            mode = "rag" if answer else "retrieval_only"
        except Exception as e:
            print(f"[notes_rag] LLM 생성 실패(무시): {_safe_err(e)}", file=sys.stderr)
    # 응답엔 본문 text 대신 출처 메타만 노출(원문 보호·경량화). answer 는 sources 동반 보장.
    # 출처 칩의 매칭 본문 샘플(≤100자). 매칭 용어(질의 형태소)는 프론트가 볼드 처리.
    q_terms = [t for t in dict.fromkeys(tokenize_korean(q)) if len(t) >= 2]

    def _snippet(s):
        t = re.sub(r"\s+", " ", (s.get("text") or "").strip())
        if not t:
            return None
        # 매칭 용어가 화면에 보이도록 첫 매칭 위치로 윈도우 시작(앞 20자 여유)
        hits = [t.find(term) for term in q_terms if t.find(term) >= 0]
        if hits:
            start = max(0, min(hits) - 20)
            return ("…" if start > 0 else "") + t[start:start + 100]
        return t[:100]
    src_out = [{**{k: s.get(k) for k in ("company", "period", "fs_div", "note_no",
                                         "title", "page_start", "page_end", "match_page", "score")},
                "snippet": _snippet(s)} for s in sources]
    return {"query": q, "fs_div": fs_div, "sources": src_out, "terms": q_terms,
            "answer": answer, "mode": mode, "ollama": _OLLAMA_AVAILABLE}


# 제목 스캔 셀 상한 — 경량 응답·결정론 보장(청크 미스캔, 제목만). 카탈로그 정렬 순회.
_SUGGEST_MAX_CELLS = 32


@app.get("/api/terms/suggest")
async def terms_suggest(q: str, companies: Optional[str] = None,
                        period: Optional[str] = None, fs_div: str = "연결",
                        doc_type: Literal["report", "review", "review_sep"] = "report"):
    """검색어 동의어/관련 용어 제안(오프라인·결정론·AI 무경유).

    안전경계: 로컬 어휘만 — synonyms 그룹 + 인덱싱된 주석 '제목'. 외부 API·임베딩·랭킹 무관여.
    제목만 스캔(청크 미접근)·셀 수 캡 → 경량. 비교조회·RAG 두 검색박스 공용.
    """
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "질의가 비어 있습니다.")
    # 화이트리스트 교집합(CLAUDE.md 입력검증) — 미지/오타 회사명은 무시. 비면 전체.
    want_companies = (set((companies or "").split(",")) - {""}) & set(VALID_COMPANIES) or set(VALID_COMPANIES)

    # 제목 수집 — 인덱싱 report 셀의 notes 제목만(fs_div 필터). 결정론 위해 카탈로그 정렬 순회.
    note_titles: List[str] = []
    try:
        entries = sorted(
            (e for e in load_catalog()["entries"]
             if e.get("company") in want_companies and e.get("indexed")
             and not (period and e.get("period") != period)),
            key=lambda e: (e.get("company", ""), e.get("period", "")),
        )
        for e in entries[:_SUGGEST_MAX_CELLS]:
            idx = _load_index(e["company"], e["period"], doc_type)
            if not idx:
                continue
            for n in _notes_for_comparison(idx, fs_div):
                title = (n.get("title") or "").strip()
                if title:
                    note_titles.append(title)
    except Exception as e:
        raise HTTPException(500, _safe_err(e))

    q_tokens = tokenize_korean(q)
    result = synonyms.suggest_terms(q, q_tokens, note_titles, synonyms.expand_query)
    return {"query": q, "applied": result["applied"], "suggestions": result["suggestions"]}


# ----------------------------------------------------------------------------
# 주석 XBRL 상세태깅 진단 (§read-only) — 사전계산 JSON 만 읽음(파싱 0).
# 대용량 instance 파싱은 scripts/build_xbrl_tagging.py 가 1회 수행 →
# storage/.../xbrl_tagging.json. 여기선 그 결과를 그대로 노출(숫자 AI 무경유·결정론).
# ----------------------------------------------------------------------------
def _load_xbrl_tagging(company: str, period: str) -> Optional[dict]:
    """셀의 사전계산 진단 JSON 로드. 없으면 None(미생성 셀 안전 처리)."""
    p = entry_dir(company, period) / "xbrl_tagging.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/xbrl-tagging")
async def xbrl_tagging_cell(company: str, period: str):
    """1셀 주석 XBRL 태깅 진단(L1 범위·L2 충실도·L3 PDF대조) — 사전계산 결과."""
    validate_company(company)
    validate_period(period)
    diag = _load_xbrl_tagging(company, period)
    if diag is None:
        return {"company": company, "period": period, "status": "not_built",
                "hint": "scripts/build_xbrl_tagging.py build 로 사전계산 필요"}
    return diag


@app.get("/api/xbrl-tagging/matrix")
async def xbrl_tagging_matrix():
    """4사×분기 횡단 요약 — 기간편차(used_ratio·fact깊이·textblock·L3) 한 표.

    카탈로그 엔트리 순회 + 사전계산 JSON 읽기만(파싱 0).
    """
    rows = []
    try:
        for e in load_catalog()["entries"]:
            co, pd = e.get("company"), e.get("period")
            diag = _load_xbrl_tagging(co, pd)
            if diag is None:
                rows.append({"company": co, "period": pd, "status": "not_built"})
                continue
            if diag.get("status") != "ok":
                rows.append({"company": co, "period": pd, "status": diag.get("status")})
                continue
            t = diag["l2"]["totals"]
            rows.append({
                "company": co, "period": pd, "status": "ok",
                "facts": t["facts"], "numeric": t["numeric"], "textblock": t["textblock"],
                "max_axes": t.get("max_axes"),  # 최대 차원수(상세태깅 정교함, dimension-aware)
                "notes_used": diag["l1"]["notes_used"],
                "notes_declared": diag["l1"]["notes_declared"],
                "used_ratio": diag["l1"]["used_ratio"],
                "l3_rate": diag["l3"]["overall"]["rate"],
                "l3_matched": diag["l3"]["overall"]["matched"],
                "l3_total": diag["l3"]["overall"]["total"],
            })
    except Exception as e:
        raise HTTPException(500, _safe_err(e))
    rows.sort(key=lambda r: (r["company"], r["period"]))
    return {"rows": rows}


# 정적 파일 서빙
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
