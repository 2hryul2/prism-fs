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

from paths import LIBRARY_ROOT  # dev·frozen 일관 storage 경로

BASE_DIR = Path(__file__).resolve().parent

MAX_PAGES_PER_NOTE = 3      # 노트당 추출 페이지 상한(예: page_start..+2)
MAX_CHARS_PER_NOTE = 2000   # 노트당 텍스트 글자 상한(LLM 컨텍스트 폭주 방지)


def _cosine(a, b) -> float:
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def _best_unit(query_emb, note: Dict[str, Any]):
    """노트의 제목 + 본문 청크 중 질의와 최고 cosine 유닛(점수·page·text).

    text 는 최고 유닛이 청크일 때만 채워짐(제목이 최고면 None → 호출부에서 페이지 추출 폴백).
    구(舊) 인덱스(chunks 부재)는 제목 임베딩만 평가(하위호환).
    """
    best_s = _cosine(query_emb, note["embedding"]) if note.get("embedding") else -1.0
    best_page = note.get("page_start")
    best_text = None
    for ch in note.get("chunks", []):
        e = ch.get("embedding")
        if not e:
            continue
        s = _cosine(query_emb, e)
        if s > best_s:
            best_s, best_page, best_text = s, ch.get("page", best_page), ch.get("text")
    return best_s, best_page, best_text


def retrieve(query_emb: List[float], indexed_cells: List[Dict[str, Any]],
             fs_div: str = "연결", top_k: int = 5, note_kind: str = "전체",
             query_text: Optional[str] = None, tokenize=None, expand=None, bm25_cls=None,
             cos_w: float = 0.7, cos_w_policy: float = 0.85,
             title_bonus: float = 0.15) -> List[Dict[str, Any]]:
    """질의 임베딩 ↔ 각 노트의 제목+본문청크 cosine(최댓값). 동일 fs_div·kind 중 상위 K.

    Phase C: 제목만이 아니라 본문 청크까지 평가 → 내용 질의 매칭. match_page/text 동봉
    (최고 청크의 실제 페이지·원문) → 인용 페이지 정밀화. 구 인덱스는 제목-only 폴백.

    하이브리드(옵트인): query_text·tokenize·bm25_cls 가 모두 주입되면 BM25(제목+청크 토큰)를
    동의어 확장(expand) 질의로 산출해 cosine 과 블렌딩한다. 가중은 note_kind 인지 —
    주기(정책 서술)는 임베딩 비중 cos_w_policy, 그 외는 cos_w(나머지는 BM25). 순환 import 회피
    위해 tokenize/expand/bm25_cls 는 호출부(app)에서 주입. 미주입 시 cosine-only(하위호환).

    indexed_cells: [{company, period, index}] (index = index.json dict)
    반환: [{company, period, fs_div, note_no, title, page_start, page_end, match_page, text, score}]
    """
    try:
        import note_filters
    except ImportError:
        note_filters = None
    cands: List[Dict[str, Any]] = []
    cos_list: List[float] = []
    corpus: List[List[str]] = []
    title_toks_list: List[set] = []  # 제목 토큰(고신호 — 변별 보너스용)
    for cell in indexed_cells:
        idx = cell.get("index") or {}
        for n in idx.get("notes", []):
            if fs_div != "all" and n.get("fs_div") and n.get("fs_div") != fs_div:
                continue
            if note_filters and not note_filters.matches_kind(n.get("title", ""), note_kind):
                continue
            if not n.get("embedding") and not n.get("chunks"):
                continue
            score, page, text = _best_unit(query_emb, n)
            cos_list.append(score)
            # BM25 코퍼스 토큰 = 제목 토큰 + 청크 토큰(인덱스에 사전계산). 없으면 tokenize 폴백.
            ttoks = list(n.get("tokens") or (tokenize(n.get("title", "")) if tokenize else []))
            toks = list(ttoks)
            for ch in n.get("chunks", []):
                toks.extend(ch.get("tokens") or [])
            corpus.append(toks)
            title_toks_list.append(set(ttoks))
            cands.append({
                "company": cell["company"], "period": cell["period"],
                "fs_div": n.get("fs_div") or fs_div,
                "note_no": n.get("no"), "title": n.get("title"),
                "page_start": n.get("page_start"), "page_end": n.get("page_end"),
                "match_page": page, "text": text,
                "score": round(score, 4),
            })

    # 하이브리드 재점수(옵트인). 미주입이면 cosine 점수 그대로.
    if query_text and tokenize and bm25_cls and cands:
        import numpy as np
        q_tokens = tokenize(expand(query_text) if expand else query_text)
        q_set = set(q_tokens)
        bm25 = bm25_cls([c or ["_"] for c in corpus])  # 빈 토큰 방지
        bm_scores = np.array(bm25.get_scores(q_tokens))
        bm_norm = bm_scores / max(bm_scores.max(), 1e-9)
        # 제목 토큰 IDF — 후보 제목들에 흔한 토큰(금융·자산 등)은 변별력↓ → 보너스 가중↓.
        import math
        N = len(cands)
        df: Dict[str, int] = {}
        for tset in title_toks_list:
            for t in (tset & q_set):
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log((N + 1) / (df.get(t, 0) + 1)) + 1.0 for t in q_set}
        idf_total = sum(idf.values()) or 1.0
        for i, c in enumerate(cands):
            kind = note_filters.note_kind(c["title"] or "") if note_filters else "서술형"
            w = cos_w_policy if kind == "주기" else cos_w
            base = w * cos_list[i] + (1.0 - w) * bm_norm[i]
            # 제목 일치 보너스(IDF 가중) — 질의어가 제목에 직접 등장할수록 가산하되, 희소(변별력
            # 높은) 토큰 일치를 우대. 유사 제목 변별(회계정책 vs 회계추정·FVOCI 표적 구분).
            ov = sum(idf[t] for t in (q_set & title_toks_list[i])) / idf_total
            c["score"] = round(float(base + title_bonus * ov), 4)

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
        pg = s.get("match_page") or s.get("page_start")
        tag = f"[출처: {s['company']} {s['period']} {s.get('fs_div','')} 주석{s.get('note_no')} p{pg}]"
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
        pg = s.get("match_page") or s.get("page_start")
        tag = f"[출처: {s['company']} {s['period']} {s.get('fs_div','')} 주석{s.get('note_no')} p{pg}]"
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
