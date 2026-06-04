"""rank_notes_for_query 의 정렬·상한·낮은신뢰도 포함·결정론 단위 테스트.

USE_BM25 를 끄면 final=cosine 이라 점수를 결정론으로 통제(모델·BM25 불요).
브리프 C1: top-k score 내림차순, k 상한, keep=False(낮은 신뢰도) 포함, 동점 note_no asc.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def _note(no, title, emb):
    return {"no": no, "title": title, "page_start": 10, "page_end": 12,
            "fs_div": "연결", "embedding": emb, "chunks": []}


# q=[1,0] 기준 cosine 통제용 단위벡터(코사인 = 첫 성분).
_Q = [1.0, 0.0]
_EMB_090 = [0.90, 0.43589]   # cosine ≈ 0.90 (high 후보)
_EMB_060 = [0.60, 0.80]      # cosine = 0.60
_EMB_030 = [0.30, 0.95394]   # cosine ≈ 0.30 (낮은 신뢰도·keep 경계)
_EMB_010 = [0.10, 0.99499]   # cosine ≈ 0.10 (확실한 낮은 신뢰도)


def test_sorted_desc_and_k_cap(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)  # score=cosine
    notes = [
        _note(1, "파생상품", _EMB_010),
        _note(2, "리스", _EMB_090),
        _note(3, "법인세", _EMB_060),
        _note(4, "영업권", _EMB_030),
    ]
    res = app.rank_notes_for_query(_Q, "리스", notes, k=2)
    assert len(res) == 2                       # k 상한
    assert res[0]["note_no"] == 2              # 최고점 먼저
    assert res[1]["note_no"] == 3
    assert res[0]["score"] >= res[1]["score"]  # 내림차순


def test_low_confidence_included(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)
    # 모두 낮은 점수(서술형, lexical 없음) → keep=False·confidence=low 여도 후보에 포함.
    notes = [
        _note(1, "파생상품", _EMB_010),
        _note(2, "영업권", _EMB_030),
    ]
    res = app.rank_notes_for_query(_Q, "전혀무관단어", notes, k=5)
    assert len(res) == 2                       # 낮은 신뢰도도 숨기지 않음
    assert all(c["confidence"] == "low" for c in res)
    assert any(c["keep"] is False for c in res)
    assert res[0]["score"] >= res[1]["score"]


def test_tiebreak_note_no_asc(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)
    # 동일 임베딩 → 동점 → note_no 오름차순(결정론).
    notes = [
        _note(5, "파생상품", _EMB_060),
        _note(2, "리스", _EMB_060),
        _note(9, "법인세", _EMB_060),
    ]
    res = app.rank_notes_for_query(_Q, "", notes, k=5)
    assert [c["note_no"] for c in res] == [2, 5, 9]


def test_score_rounded_and_deterministic(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)
    notes = [_note(1, "리스", _EMB_090), _note(2, "법인세", _EMB_060)]
    r1 = app.rank_notes_for_query(_Q, "리스", notes, k=5)
    r2 = app.rank_notes_for_query(_Q, "리스", notes, k=5)
    assert r1 == r2                            # 동일 입력 → 동일 출력(결정론)
    assert r1[0]["score"] == round(r1[0]["score"], 3)


def test_empty_notes_returns_empty(monkeypatch):
    monkeypatch.setattr(app, "USE_BM25", False)
    assert app.rank_notes_for_query(_Q, "리스", [], k=5) == []
