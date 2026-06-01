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
from typing import List

# 동의어 그룹: 한 그룹 내 용어는 검색 시 상호 확장.
_SYNONYM_GROUPS: List[set] = [
    {"대손충당금", "대손준비금", "신용손실충당금", "손실충당금", "기대신용손실"},
    {"종속기업", "자회사"},
    {"관계기업", "지분법피투자기업"},
    {"영업권", "굿윌"},
    {"유형자산", "고정자산"},
    {"무형자산", "지적재산"},
    {"사채", "회사채", "차입금"},
    {"리스", "임차"},
    {"당기순이익", "분기순이익", "반기순이익", "당기순손익"},
    {"공정가치", "공정가액"},
    {"퇴직급여", "확정급여"},
    {"우발부채", "우발채무"},
    {"특수관계자", "특수관계인"},
    {"파생상품", "파생금융상품"},
]


def _has_term(q_text: str, term: str) -> bool:
    """용어가 질의에 '단어'로 등장하는지 — 앞뒤가 한글이면 더 긴 합성어의 일부로 보고 제외.

    예: '리스'는 '리스크관리' 안에서는 매칭되지 않음(오확장 방지), '리스 회계'에서는 매칭.
    """
    return re.search(rf"(?<![가-힣]){re.escape(term)}(?![가-힣])", q_text) is not None


def expand_query(q_text: str) -> str:
    """질의에 등장한 용어의 동의어를 덧붙인 텍스트 반환(BM25·lexical 용). 원질의 보존+추가."""
    if not q_text:
        return q_text
    extra: List[str] = []
    for group in _SYNONYM_GROUPS:
        if any(_has_term(q_text, term) for term in group):
            extra.extend(t for t in group if t not in q_text)
    return f"{q_text} {' '.join(extra)}" if extra else q_text
