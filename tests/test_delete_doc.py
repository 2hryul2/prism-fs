"""문서유형별 삭제 + 셀 전체 삭제 단위테스트.

검증 범위:
- _strip_doc_fields: review 삭제가 review_sep 데이터를 보존(접두 충돌 — 최우선)
- delete_library_doc 엔드포인트: 형제 문서 보존, 마지막 문서±재무데이터 분기, 잘못된 doc_type
디스크는 tmp_path 로 격리(app.LIBRARY_ROOT/CATALOG_PATH 몽키패치), 네트워크 없음.
"""
import sys
import json
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


# ── _strip_doc_fields (순수) ─────────────────────────────────────────────────
def test_strip_review_preserves_review_sep():
    """review 삭제 시 review_sep_* 는 반드시 보존(접두 충돌 방지)."""
    entry = {
        "company": "신한", "period": "2025Q3",
        "indexed": True, "notes_count": 50,
        "review_indexed": True, "review_notes_count": 30,
        "review_sep_indexed": True, "review_sep_notes_count": 31,
    }
    out = app._strip_doc_fields(entry, "review")
    # review_* 제거
    assert "review_indexed" not in out and "review_notes_count" not in out
    # review_sep_* 보존 (핵심)
    assert out["review_sep_indexed"] is True and out["review_sep_notes_count"] == 31
    # report 보존
    assert out["indexed"] is True and out["notes_count"] == 50


def test_strip_report_keeps_reviews():
    entry = {"company": "신한", "period": "2025Q3", "indexed": True, "notes_count": 50,
             "review_indexed": True, "review_sep_indexed": True}
    out = app._strip_doc_fields(entry, "report")
    assert "indexed" not in out and "notes_count" not in out
    assert out["review_indexed"] is True and out["review_sep_indexed"] is True


def test_strip_review_sep_only():
    entry = {"company": "신한", "period": "2025Q3", "review_indexed": True,
             "review_sep_indexed": True, "review_sep_notes_count": 31}
    out = app._strip_doc_fields(entry, "review_sep")
    assert "review_sep_indexed" not in out and "review_sep_notes_count" not in out
    assert out["review_indexed"] is True  # 연결은 보존


# ── 엔드포인트 (tmp 디스크 격리) ─────────────────────────────────────────────
@pytest.fixture
def lib(tmp_path, monkeypatch):
    """app 의 라이브러리 루트·카탈로그를 tmp 로 격리."""
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(app, "LIBRARY_ROOT", root)
    monkeypatch.setattr(app, "CATALOG_PATH", tmp_path / "catalog.json")
    return root


def _seed(root, company, period, doc_types, fs=False):
    """셀에 작업본 PDF·인덱스 파일과 카탈로그 엔트리를 만든다."""
    d = root / company / period
    d.mkdir(parents=True, exist_ok=True)
    fields = {}
    for dt in doc_types:
        app.pdf_path(company, period, dt).write_bytes(b"%PDF-1.4 test")
        app.index_path(company, period, dt).write_text("{}", encoding="utf-8")
        if dt == "report":
            fields.update(indexed=True, notes_count=10)
        elif dt == "review":
            fields.update(review_indexed=True, review_notes_count=20)
        else:
            fields.update(review_sep_indexed=True, review_sep_notes_count=30)
    if fs:
        (d / "fs_structured.json").write_text("{}", encoding="utf-8")
        fields["fs_collected"] = True
    app.upsert_catalog_entry(company, period, **fields)
    return d


def test_delete_doc_keeps_siblings(lib):
    d = _seed(lib, "신한", "2025Q3", ["report", "review"])
    res = asyncio.run(app.delete_library_doc("신한", "2025Q3", "review"))
    assert res["scope"] == "review"
    # review 삭제·report 보존
    assert not app.pdf_path("신한", "2025Q3", "review").exists()
    assert not app.index_path("신한", "2025Q3", "review").exists()
    assert app.pdf_path("신한", "2025Q3", "report").exists()
    entry = next(e for e in app.load_catalog()["entries"]
                 if e["company"] == "신한" and e["period"] == "2025Q3")
    assert entry.get("indexed") is True
    assert "review_indexed" not in entry


def test_delete_last_doc_no_fs_removes_cell(lib):
    d = _seed(lib, "KB", "2025Q3", ["report"])
    res = asyncio.run(app.delete_library_doc("KB", "2025Q3", "report"))
    assert res["scope"] == "cell"
    assert not d.exists()
    assert not any(e["company"] == "KB" and e["period"] == "2025Q3"
                   for e in app.load_catalog()["entries"])


def test_delete_last_doc_with_fs_keeps_cell(lib):
    d = _seed(lib, "하나", "2025FY", ["review"], fs=True)
    res = asyncio.run(app.delete_library_doc("하나", "2025FY", "review"))
    assert res["scope"] == "review"          # 셀 유지(재무데이터 존재)
    assert d.exists()
    assert (d / "fs_structured.json").exists()
    entry = next(e for e in app.load_catalog()["entries"]
                 if e["company"] == "하나" and e["period"] == "2025FY")
    assert entry.get("fs_collected") is True
    assert "review_indexed" not in entry


def test_delete_bad_doc_type_400(lib):
    _seed(lib, "신한", "2025Q3", ["report"])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app.delete_library_doc("신한", "2025Q3", "bogus"))
    assert ei.value.status_code == 400


def test_delete_missing_cell_404(lib):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app.delete_library_doc("우리", "2025Q3", "report"))
    assert ei.value.status_code == 404
