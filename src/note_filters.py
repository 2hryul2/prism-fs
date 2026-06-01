"""
note_filters.py — 주석 종류(주기/서술형) 결정론 분류 · prism-fs

주석 제목 키워드 룰셋으로 kind 태깅. AI 무경유. compare/coverage/RAG 필터에 공용 사용.
- 주기(회계정책): 회계정책·작성기준·추정·판단 등 정책/기준 서술.
- 서술형: 그 외 항목 서술(위험·약정·우발·계정 상세 등).
"""
from __future__ import annotations

# 주기(회계정책) 판별 키워드 — 제목에 하나라도 포함되면 "주기".
_POLICY_KEYWORDS = (
    "회계정책", "작성기준", "작성 기준", "중요한 회계", "회계기준", "측정기준",
    "추정", "판단", "일반사항", "회사의 개요", "보고주체", "재무제표 작성",
    "적용", "변경", "제·개정", "제개정",
)


def note_kind(title: str) -> str:
    """주석 제목 → "주기" | "서술형" (결정론 키워드 규칙)."""
    t = title or ""
    return "주기" if any(k in t for k in _POLICY_KEYWORDS) else "서술형"


def matches_kind(title: str, kind: str) -> bool:
    """kind 필터 통과 여부. kind in {"전체","주기","서술형"}."""
    if kind in (None, "", "전체", "all"):
        return True
    return note_kind(title) == kind


def filter_notes(notes: list, kind: str) -> list:
    """note 리스트를 kind 로 필터(title 기준). 전체면 그대로."""
    if kind in (None, "", "전체", "all"):
        return notes
    return [n for n in notes if note_kind(n.get("title", "")) == kind]
