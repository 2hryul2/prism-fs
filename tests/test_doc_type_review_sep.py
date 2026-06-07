"""review_sep(별도검토보고서) 3-문서 모델 — 경로 매핑·원본명 폴백 단위테스트.

검증 범위:
- pdf_path/index_path 의 review_sep 매핑(report/review 불변)
- validate_doc_type("review_sep") 통과
- original_pdf_name 폴백 스캔이 표준 작업본(report.pdf 등)을 원본으로 오인하지 않음
- original_pdf_name meta.json documents[] 우선
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


def test_validate_doc_type_review_sep():
    assert app.validate_doc_type("review_sep") == "review_sep"
    # 하위호환: 미지정/빈값 → report
    assert app.validate_doc_type(None) == "report"
    assert app.validate_doc_type("") == "report"


def test_pdf_index_path_mapping(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "LIBRARY_ROOT", tmp_path)
    c, p = "신한", "2025Q3"
    # report/review 파일명 불변 + review_sep 신규 매핑
    assert app.pdf_path(c, p, "report").name == "report.pdf"
    assert app.pdf_path(c, p, "review").name == "review.pdf"
    assert app.pdf_path(c, p, "review_sep").name == "review_sep.pdf"
    assert app.index_path(c, p, "report").name == "index.json"
    assert app.index_path(c, p, "review").name == "index_review.json"
    assert app.index_path(c, p, "review_sep").name == "index_review_sep.json"


def test_original_pdf_name_fallback_excludes_standard(monkeypatch, tmp_path):
    """폴백 스캔 — 표준 작업본 3종은 제외, bracketed 원본만 키워드 매칭."""
    monkeypatch.setattr(app, "LIBRARY_ROOT", tmp_path)
    c, p = "KB", "2025Q3"
    d = tmp_path / c / p
    d.mkdir(parents=True)
    # 표준 작업본(오인 대상) + 원본 3종 합성
    for fn in ("report.pdf", "review.pdf", "review_sep.pdf",
               "[KB금융]분기보고서(2025.09).pdf",
               "[KB금융]연결검토보고서(2025.09).pdf",
               "[KB금융]검토보고서(2025.09).pdf"):
        (d / fn).write_bytes(b"%PDF-1.4 test")

    # report: 분기보고서 & 검토 미포함 → 표준 report.pdf 가 아닌 bracketed 원본
    assert app.original_pdf_name(c, p, "report") == "[KB금융]분기보고서(2025.09).pdf"
    # review: 연결검토 포함
    assert app.original_pdf_name(c, p, "review") == "[KB금융]연결검토보고서(2025.09).pdf"
    # review_sep: 검토 포함 & 연결 미포함 → 연결검토본은 배제
    assert app.original_pdf_name(c, p, "review_sep") == "[KB금융]검토보고서(2025.09).pdf"
    # 어떤 결과도 표준 작업본 파일명이 아님
    for dt in ("report", "review", "review_sep"):
        assert app.original_pdf_name(c, p, dt) not in app._STANDARD_PDF_NAMES


def test_original_pdf_name_meta_priority(monkeypatch, tmp_path):
    """meta.json documents[] 의 filename_original 이 폴백보다 우선."""
    monkeypatch.setattr(app, "LIBRARY_ROOT", tmp_path)
    c, p = "하나", "2025Q3"
    d = tmp_path / c / p
    d.mkdir(parents=True)
    (d / "review_sep.pdf").write_bytes(b"%PDF-1.4 test")  # 표준 작업본만 존재
    meta = {"documents": [
        {"doc_type": "review_sep", "filename_original": "[하나금융]별도검토_원본.pdf"},
    ]}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert app.original_pdf_name(c, p, "review_sep") == "[하나금융]별도검토_원본.pdf"


def test_original_pdf_name_none_when_empty(monkeypatch, tmp_path):
    """원본 후보가 없으면 None(표준 작업본만 있어도 None)."""
    monkeypatch.setattr(app, "LIBRARY_ROOT", tmp_path)
    c, p = "우리", "2025Q3"
    d = tmp_path / c / p
    d.mkdir(parents=True)
    (d / "report.pdf").write_bytes(b"%PDF-1.4 test")
    assert app.original_pdf_name(c, p, "review_sep") is None
