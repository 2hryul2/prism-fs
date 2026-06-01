"""synonyms 동의어 쿼리 확장(Phase E) — 결정론·원질의 보존 검증."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import synonyms  # noqa: E402


def test_expands_company_variant_terms():
    out = synonyms.expand_query("대손충당금 산정")
    assert "대손충당금 산정" in out          # 원질의 보존
    assert "대손준비금" in out               # 회사변형 동의어 추가
    assert "기대신용손실" in out


def test_no_match_returns_original():
    assert synonyms.expand_query("리스크관리 일반") == "리스크관리 일반"


def test_empty_safe():
    assert synonyms.expand_query("") == ""


def test_distinct_concepts_not_grouped():
    """의미가 다른 영업이익/영업수익은 묶지 않음(오답 방지)."""
    out = synonyms.expand_query("영업이익 분석")
    assert "영업수익" not in out
