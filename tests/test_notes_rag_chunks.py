"""notes_rag 청크 기반 검색(Phase A/C) 검증 — 합성 임베딩으로 결정론 단언(모델 불요)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import notes_rag  # noqa: E402


def _note(no, title, title_emb, chunks):
    return {"no": no, "title": title, "fs_div": "연결",
            "page_start": 10, "page_end": 20, "embedding": title_emb, "chunks": chunks}


def test_chunk_beats_title_and_returns_match_page():
    """제목은 무관하지만 본문 청크가 질의와 일치 → 청크로 매칭 + match_page=청크 페이지 + text."""
    q = [1.0, 0.0, 0.0]
    notes = [
        _note(1, "무관한 제목", [0.0, 1.0, 0.0], [
            {"text": "질의와 일치하는 본문", "page": 13, "embedding": [0.99, 0.01, 0.0]},
            {"text": "딴 내용", "page": 14, "embedding": [0.0, 0.0, 1.0]},
        ]),
        _note(2, "다른 주석", [0.0, 0.0, 1.0], [
            {"text": "무관", "page": 30, "embedding": [0.0, 1.0, 0.0]},
        ]),
    ]
    cells = [{"company": "신한", "period": "2026Q1", "index": {"notes": notes}}]
    got = notes_rag.retrieve(q, cells, fs_div="연결", top_k=2)
    assert got[0]["note_no"] == 1
    assert got[0]["match_page"] == 13          # 최고 청크의 실제 페이지(인용 정밀)
    assert got[0]["text"] == "질의와 일치하는 본문"
    assert got[0]["score"] > got[1]["score"]


def test_title_fallback_when_no_chunk_better():
    """제목 임베딩이 최고면 text=None(호출부가 페이지 추출 폴백) + match_page=page_start."""
    q = [0.0, 1.0, 0.0]
    notes = [_note(5, "제목매칭", [0.0, 1.0, 0.0], [
        {"text": "무관", "page": 18, "embedding": [1.0, 0.0, 0.0]},
    ])]
    cells = [{"company": "KB", "period": "2026Q1", "index": {"notes": notes}}]
    got = notes_rag.retrieve(q, cells, fs_div="연결", top_k=1)
    assert got[0]["note_no"] == 5
    assert got[0]["text"] is None
    assert got[0]["match_page"] == 10          # page_start


def test_legacy_index_without_chunks_still_works():
    """구 인덱스(chunks 부재)도 제목 임베딩만으로 동작(하위호환)."""
    q = [1.0, 0.0]
    notes = [{"no": 1, "title": "구주석", "fs_div": "연결",
              "page_start": 5, "page_end": 9, "embedding": [1.0, 0.0]}]
    cells = [{"company": "하나", "period": "2025Q2", "index": {"notes": notes}}]
    got = notes_rag.retrieve(q, cells, fs_div="연결", top_k=1)
    assert got[0]["note_no"] == 1 and got[0]["text"] is None
