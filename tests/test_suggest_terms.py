"""suggest_terms(C2) 단위테스트 — 동의어 멤버·코퍼스 제목 제안, applied diff,
q중복 제외, 결정론. 합성 titles + 가짜/실 expand_fn(외부 의존 0)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import synonyms  # noqa: E402


def _fake_expand(q):
    """결정론 가짜 확장 — '대손충당금' 질의에만 동의어 2개 덧붙임(원문 보존)."""
    if "대손충당금" in q:
        return f"{q} 대손준비금 기대신용손실"
    return q


# --- applied(반영된 동의어) ---------------------------------------------------
def test_applied_is_expand_diff():
    out = synonyms.suggest_terms("대손충당금 산정", ["대손충당금", "산정"], [], _fake_expand)
    assert out["applied"] == ["대손준비금", "기대신용손실"]  # 원문 대비 추가분만, 순서 보존


def test_applied_empty_when_no_expansion():
    out = synonyms.suggest_terms("리스크관리", ["리스크관리"], [], _fake_expand)
    assert out["applied"] == []


# --- synonym 제안(그룹 다른 멤버) --------------------------------------------
def test_synonym_member_suggestion():
    # 실제 _SYNONYM_GROUPS 사용 — 대손충당금 그룹의 다른 멤버가 제안돼야 함.
    out = synonyms.suggest_terms("대손충당금", ["대손충당금"], [], synonyms.expand_query)
    syn = [s["term"] for s in out["suggestions"] if s["kind"] == "synonym"]
    assert "대손준비금" in syn
    assert "대손충당금" not in syn  # q 에 있는 멤버는 제외


# --- corpus 제안(겹치는 제목) ------------------------------------------------
def test_corpus_title_suggestion_and_short_preferred():
    titles = ["공정가치 측정 및 평가 상세 내역", "공정가치", "유형자산"]
    out = synonyms.suggest_terms("공정가치", ["공정가치"], titles, lambda q: q)
    corpus = [s["term"] for s in out["suggestions"] if s["kind"] == "corpus"]
    assert "공정가치" not in corpus            # q 와 동일한 제목은 제외
    assert "공정가치 측정 및 평가 상세 내역" in corpus
    assert "유형자산" not in corpus            # 겹침 없는 제목은 제외


def test_corpus_no_overlap_returns_nothing():
    out = synonyms.suggest_terms("외화환산", ["외화환산"], ["유형자산", "재고자산"], lambda q: q)
    assert [s for s in out["suggestions"] if s["kind"] == "corpus"] == []


# --- q 중복 제외 --------------------------------------------------------------
def test_query_terms_excluded():
    titles = ["대손충당금", "대손충당금 변동내역"]
    out = synonyms.suggest_terms("대손충당금", ["대손충당금"], titles, synonyms.expand_query)
    terms = [s["term"] for s in out["suggestions"]]
    assert "대손충당금" not in terms  # 원문 그대로인 제목·동의어 멤버 모두 제외


# --- 결정론(동일 입력 → 동일 출력) -------------------------------------------
def test_deterministic_ordering():
    titles = ["공정가치 평가", "공정가치 측정", "공정가치 수준"]
    args = ("공정가치", ["공정가치"], titles, lambda q: q)
    first = synonyms.suggest_terms(*args)
    for _ in range(5):
        assert synonyms.suggest_terms(*args) == first


def test_cap_total_suggestions():
    # 동의어 + 다수 제목 → 상한(_SUGGEST_CAP) 이내.
    titles = [f"공정가치 항목 {i} 상세" for i in range(30)]
    out = synonyms.suggest_terms("공정가치", ["공정가치"], titles, synonyms.expand_query)
    assert len(out["suggestions"]) <= synonyms._SUGGEST_CAP
