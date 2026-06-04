"""
synonyms.py — 회계 주석 동의어/회사변형 용어 사전 (Phase E, prism-fs)

회사마다 같은 개념을 다른 용어로 표기(대손충당금↔대손준비금 등)하는 차이를 흡수해
**검색 재현율**을 높인다. 결정론·텍스트 전용(AI 무경유). 임베딩 질의는 원문 유지하고,
BM25/lexical 매칭용 질의 텍스트에만 동의어를 덧붙인다(정밀도 훼손 최소화).

보수적 원칙: **진짜 동의어 / 회사별 표기 변형만** 포함. 의미가 다른 용어
(예: 영업이익 vs 영업수익)는 묶지 않는다 — 잘못 묶으면 오답을 유발한다.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Callable, Dict, List

# 동의어 그룹은 외부 데이터(synonyms_data.json)에서 로드 — 비개발자(회계팀) 편집 가능.
# 회사 환경 우선순위: storage/synonyms.json(배포 후 오버라이드) > src/synonyms_data.json(번들).
# 영문 약어(ECL/FVOCI 등)는 한글 정식명과 같은 그룹 → 약어 질의 시 한글 표기를 BM25에 덧붙임.
_FALLBACK_GROUPS = [
    ["대손충당금", "대손준비금", "신용손실충당금", "손실충당금", "기대신용손실", "ECL"],
    ["파생상품", "파생금융상품"],
]


def _load_groups() -> List[set]:
    """synonyms_data.json(또는 storage 오버라이드) 로드. 실패 시 최소 폴백."""
    here = Path(__file__).resolve().parent
    candidates = [here / "synonyms_data.json"]
    try:  # storage 오버라이드(배포 후 편집분 우선) — paths 미가용 시 무시
        from paths import STORAGE_ROOT
        candidates.insert(0, Path(STORAGE_ROOT) / "synonyms.json")
    except Exception:
        pass
    for p in candidates:
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                groups = [set(g) for g in data.get("groups", []) if len(g) >= 2]
                if groups:
                    return groups
        except Exception as e:  # 파손 시 폴백(검색 중단 방지)
            print(f"[synonyms] {p} 로드 실패 → 폴백: {e}")
    return [set(g) for g in _FALLBACK_GROUPS]


_SYNONYM_GROUPS: List[set] = _load_groups()


def _has_term(q_text: str, term: str) -> bool:
    """용어가 질의에 '단어'로 등장하는지 — 앞뒤가 한글/영숫자면 더 긴 합성어의 일부로 보고 제외.

    한글 예: '리스'는 '리스크관리' 안에서는 매칭 안 됨(오확장 방지), '리스 회계'에서는 매칭.
    영문 예: 'ECL'은 'ECLS' 안에서는 매칭 안 됨. 약어는 대소문자 무시(re.IGNORECASE).
    """
    return re.search(
        rf"(?<![0-9A-Za-z가-힣]){re.escape(term)}(?![0-9A-Za-z가-힣])",
        q_text, re.IGNORECASE,
    ) is not None


def expand_query(q_text: str) -> str:
    """질의에 등장한 용어의 동의어를 덧붙인 텍스트 반환(BM25·lexical 용). 원질의 보존+추가."""
    if not q_text:
        return q_text
    extra: List[str] = []
    for group in _SYNONYM_GROUPS:
        if any(_has_term(q_text, term) for term in group):
            extra.extend(t for t in group if t not in q_text)
    return f"{q_text} {' '.join(extra)}" if extra else q_text


# 제안 칩 상한 — synonym + corpus 합산. UI 가독성·결정론 응답을 위해 고정.
_SUGGEST_CAP = 10
# corpus(주석 제목) 최소 노출 슬롯 — synonym 이 상한을 독식해 제목 제안이 사라지지 않게 예약.
_CORPUS_RESERVE = 3


def suggest_terms(
    q: str,
    q_tokens: List[str],
    note_titles: List[str],
    expand_fn: Callable[[str], str],
) -> Dict[str, list]:
    """검색어 q 에 대한 회계 동의어/관련 용어 제안(오프라인·결정론·AI 무경유).

    안전경계: 로컬 어휘(synonyms 그룹 + 주석 제목)만 사용. 외부 API·임베딩·랭킹 무관여.

    Args:
        q: 원문 질의(투명성 표시·중복 제외 기준).
        q_tokens: q 를 tokenize 한 토큰(코퍼스 제목 겹침 판정용).
        note_titles: 인덱싱된 주석 제목 목록(중복 포함 가능 — 빈도 가중에 사용).
        expand_fn: 동의어 확장 함수(보통 expand_query). applied diff 산출에 사용.

    Returns:
        {"applied": [추가된 동의어...], "suggestions": [{"term","kind"}...]}.
        applied = expand_fn(q) 가 원문 대비 덧붙인 토큰(반영된 동의어, 표시용).
        suggestions = q 에 없는 동의어 멤버(kind="synonym") + 겹치는 제목(kind="corpus"),
        dedup·q제외·상한 _SUGGEST_CAP. 정렬 고정(synonym 먼저, corpus 는 rank→text).
    """
    # applied: 확장 텍스트에서 원문을 제거한 나머지 = 실제로 덧붙은 동의어 토큰.
    applied: List[str] = []
    expanded = expand_fn(q) if q else ""
    if expanded and expanded != q:
        # expand_query 는 "원문 + 추가어" 형태 → 원문 길이 이후만 취해 공백 분리.
        tail = expanded[len(q):] if expanded.startswith(q) else expanded
        seen_a = set()
        for t in tail.split():
            if t and t not in seen_a and not _has_term(q, t):
                seen_a.add(t)
                applied.append(t)

    syn_list: List[dict] = []   # kind="synonym"
    cor_list: List[dict] = []   # kind="corpus"
    seen_terms = set()  # q 의 토큰·이미 채택된 제안어 — 중복 차단

    def _in_query(term: str) -> bool:
        return _has_term(q, term)

    # 1) synonym 제안 — q 에 등장한 그룹의 다른 멤버(오확장 경계 _has_term 그대로).
    #    그룹 순서·멤버 순서 보존 → 결정론. q 에 있는 멤버·이미 채택분은 제외.
    for group in _SYNONYM_GROUPS:
        if not any(_has_term(q, term) for term in group):
            continue
        for term in group:
            if term in seen_terms or _in_query(term):
                continue
            seen_terms.add(term)
            syn_list.append({"term": term, "kind": "synonym"})

    # 2) corpus 제안 — q 토큰과 겹치는 distinct 제목. 빈도·겹침수 가중, 짧은 제목 우선.
    q_tok_set = {t for t in q_tokens if len(t) >= 2}
    title_freq: Dict[str, int] = {}
    title_overlap: Dict[str, int] = {}
    for raw in note_titles:
        title = (raw or "").strip()
        if not title:
            continue
        title_freq[title] = title_freq.get(title, 0) + 1
        if title in title_overlap:
            continue
        # 제목을 글자 그대로 포함 판정 — q 토큰 중 제목 안에 등장하는 distinct 토큰 수.
        title_overlap[title] = sum(1 for tok in q_tok_set if tok in title)

    corpus_cands = [t for t, ov in title_overlap.items() if ov > 0]
    # 정렬: 겹침수↓(많을수록 우선) → 빈도↓ → 길이↑(짧을수록 우선) → 사전순(결정론 tie-break).
    corpus_cands.sort(
        key=lambda t: (-title_overlap[t], -title_freq[t], len(t), t)
    )
    for title in corpus_cands:
        if title in seen_terms or _in_query(title):
            continue
        seen_terms.add(title)
        cor_list.append({"term": title, "kind": "corpus"})

    # corpus 최소 예약 후 synonym 우선 채움 → 둘 다 노출 보장(결정론).
    reserve = min(len(cor_list), _CORPUS_RESERVE)
    suggestions = (syn_list[:_SUGGEST_CAP - reserve] + cor_list)[:_SUGGEST_CAP]
    return {"applied": applied, "suggestions": suggestions}
