"""
notes_rag.py — 주석 RAG (§5.4, 옵트인) · prism-fs

정성(주석 텍스트) 전용 AI 레이어. **숫자는 절대 읽거나 생성하지 않는다**(정량은 fs_compare 결정론).
- retrieve: 질의 임베딩 ↔ note title 임베딩 cosine 상위 K (동일 fs_div).
- extract_note_text: report.pdf 해당 페이지에서 본문 텍스트 추출(노트당 페이지·글자 캡).
- build_prompt/answer: Ollama 옵트인 시에만. "제공 근거로만 답하고 각 문장에 출처 표기" 강제.

인용 강제·HITL·옵트인 경계는 호출부(app.py /api/notes/rag)에서 최종 보장:
sources 가 비면 answer 생성 금지, Ollama off 면 retrieval_only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import fitz  # PyMuPDF
    _HAS_FITZ = True
except ImportError:
    _HAS_FITZ = False

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

BASE_DIR = Path(__file__).resolve().parent
LIBRARY_ROOT = BASE_DIR / "storage" / "library"

MAX_PAGES_PER_NOTE = 3      # 노트당 추출 페이지 상한(예: page_start..+2)
MAX_CHARS_PER_NOTE = 2000   # 노트당 텍스트 글자 상한(LLM 컨텍스트 폭주 방지)


def _cosine(a, b) -> float:
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def retrieve(query_emb: List[float], indexed_cells: List[Dict[str, Any]],
             fs_div: str = "연결", top_k: int = 5, note_kind: str = "전체") -> List[Dict[str, Any]]:
    """질의 임베딩 ↔ 각 셀 note title 임베딩 cosine. 동일 fs_div·kind note 중 상위 K.

    indexed_cells: [{company, period, index}] (index = index.json dict)
    반환: [{company, period, fs_div, note_no, title, page_start, page_end, score}]
    """
    try:
        import note_filters
    except ImportError:
        note_filters = None
    cands: List[Dict[str, Any]] = []
    for cell in indexed_cells:
        idx = cell.get("index") or {}
        for n in idx.get("notes", []):
            if fs_div != "all" and n.get("fs_div") and n.get("fs_div") != fs_div:
                continue
            if note_filters and not note_filters.matches_kind(n.get("title", ""), note_kind):
                continue
            emb = n.get("embedding")
            if not emb:
                continue
            cands.append({
                "company": cell["company"], "period": cell["period"],
                "fs_div": n.get("fs_div") or fs_div,
                "note_no": n.get("no"), "title": n.get("title"),
                "page_start": n.get("page_start"), "page_end": n.get("page_end"),
                "score": round(_cosine(query_emb, emb), 4),
            })
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:top_k]


def extract_note_text(company: str, period: str, page_start: int, page_end: int,
                      doc_type: str = "report") -> str:
    """report.pdf 의 page_start..min(page_start+MAX_PAGES, page_end) 텍스트(캡 적용)."""
    if not _HAS_FITZ or not page_start:
        return ""
    fname = "report.pdf" if doc_type == "report" else "review.pdf"
    pdf_path = LIBRARY_ROOT / company / period / fname
    if not pdf_path.exists():
        return ""
    last = min(page_start + MAX_PAGES_PER_NOTE - 1, page_end or page_start)
    out = []
    try:
        with fitz.open(pdf_path) as doc:
            for p in range(page_start, last + 1):
                if 1 <= p <= doc.page_count:
                    out.append(doc[p - 1].get_text())
    except Exception:
        return ""
    return ("\n".join(out))[:MAX_CHARS_PER_NOTE]


def build_prompt(query: str, sources: List[Dict[str, Any]]) -> str:
    """근거 청크에 출처 라벨을 강제 주입. 숫자 생성·재계산 금지 지시."""
    blocks = []
    for s in sources:
        tag = f"[출처: {s['company']} {s['period']} {s.get('fs_div','')} 주석{s.get('note_no')} p{s.get('page_start')}]"
        blocks.append(f"{tag}\n{s.get('text','')}")
    context = "\n\n---\n\n".join(blocks)
    return (
        "당신은 4대 금융지주 재무제표 주석 비교를 돕는 분석 보조자입니다.\n"
        "아래 [근거]에 제공된 주석 텍스트만 사용해 질문에 답하세요. 규칙:\n"
        "1) 근거에 없는 내용은 추측하지 말고 '근거 없음'이라고 답하세요.\n"
        "2) 각 문장 끝에 사용한 출처 라벨([출처: ...])을 반드시 표기하세요.\n"
        "3) 금액·숫자를 새로 계산하거나 생성하지 마세요. 숫자는 근거에 적힌 그대로만 인용하세요.\n"
        "4) 한국어로 간결하게 답하세요.\n\n"
        f"[질문]\n{query}\n\n[근거]\n{context}\n\n[답변]\n"
    )


async def _generate(prompt: str, *, openai_key: Optional[str] = None,
                    ollama_url: str = "", ollama_model: str = "",
                    openai_model: str = "gpt-4o-mini", num_predict: int = 800) -> Optional[str]:
    """LLM 생성 — OpenAI 키 있으면 OpenAI 우선, 없으면 로컬 Ollama. 둘 다 없으면 None.
    키는 헤더로만 전송(로그·예외에 노출 금지)."""
    if not _HAS_HTTPX:
        return None
    # 1) OpenAI 옵트인(키 존재 시)
    if openai_key:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    json={"model": openai_model, "temperature": 0.0,
                          "messages": [{"role": "user", "content": prompt}]},
                )
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip() or None
        except Exception:
            pass  # OpenAI 실패 시 Ollama 폴백
    # 2) 로컬 Ollama
    if ollama_url and ollama_model:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                r = await client.post(
                    f"{ollama_url}/api/generate",
                    json={"model": ollama_model, "prompt": prompt, "stream": False,
                          "options": {"temperature": 0.0, "num_predict": num_predict, "top_p": 1.0}},
                )
                r.raise_for_status()
                return (r.json().get("response") or "").strip() or None
        except Exception:
            return None
    return None


def build_compare_prompt(topic: str, sources: List[Dict[str, Any]]) -> str:
    """§5.3 비교 메모 — 회사 간 정책·가정 차이 정리. 출처 강제·숫자 생성 금지."""
    blocks = []
    for s in sources:
        tag = f"[출처: {s['company']} {s['period']} {s.get('fs_div','')} 주석{s.get('note_no')} p{s.get('page_start')}]"
        blocks.append(f"{tag}\n{(s.get('text','') or '')[:1200]}")  # 비교 컨텍스트 축약(속도)
    context = "\n\n---\n\n".join(blocks)
    return (
        "당신은 4대 금융지주 주석을 비교하는 회계 분석 보조자입니다.\n"
        f"주제 '{topic}'에 대해 아래 [근거] 주석들을 회사별로 비교하는 초안을 작성하세요. 규칙:\n"
        "1) 회사 간 **회계정책·추정 가정·공시 범위의 차이**를 중심으로 정리하세요.\n"
        "2) 근거에 있는 내용만 쓰고, 각 문장 끝에 출처 라벨([출처: ...])을 표기하세요.\n"
        "3) 금액·숫자를 새로 계산/생성하지 말고, 필요한 경우 근거에 적힌 그대로만 인용하세요.\n"
        "4) 마지막에 '⚠️ 본 초안은 검토 필요(human-in-the-loop)' 한 줄을 덧붙이세요.\n\n"
        f"[근거]\n{context}\n\n[비교 초안]\n"
    )


async def answer_compare_ollama(topic: str, sources: List[Dict[str, Any]],
                                url: str, model: str, openai_key: Optional[str] = None) -> Optional[str]:
    """비교 메모 초안(OpenAI 옵트인 우선 → Ollama). sources 없으면 None(인용 강제는 호출부)."""
    if not sources:
        return None
    return await _generate(build_compare_prompt(topic, sources), openai_key=openai_key,
                           ollama_url=url, ollama_model=model, num_predict=800)


async def answer_ollama(query: str, sources: List[Dict[str, Any]],
                        url: str, model: str, openai_key: Optional[str] = None) -> Optional[str]:
    """RAG 답변(OpenAI 옵트인 우선 → Ollama). sources 없으면 None(인용 강제는 호출부)."""
    if not sources:
        return None
    return await _generate(build_prompt(query, sources), openai_key=openai_key,
                           ollama_url=url, ollama_model=model, num_predict=800)
