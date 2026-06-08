"""업로드 PDF 자동 감지(회사/기간/문서유형) 단위테스트.

검증 범위(app.detect_* 순수 함수):
- 회사 감지(신한/KB/하나/우리, KB금융 변형 포함)
- 문서유형 감지(report/review/review_sep, 연결감사·별도감사 확장)
- 기간 감지(사업보고서→FY, 반기→Q2, 분기→월별 Q1/Q3, 월불명→Q3)
- 파일명 통합 감지 + 엣지([정정] 접두, 원본명 없음)
네트워크·디스크 없음(순수 함수).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import app  # noqa: E402


# ── 회사 감지 ───────────────────────────────────────────────────────────────
def test_company_detect_basic():
    assert app.detect_company_from_text("[신한지주]분기보고서(2025.11.14).pdf") == "신한"
    assert app.detect_company_from_text("[하나금융지주]반기보고서.pdf") == "하나"
    assert app.detect_company_from_text("[우리금융지주]분기보고서.pdf") == "우리"


def test_company_detect_kb_variants():
    # KB금융(공식 corp_name)·KB 양쪽 인식
    assert app.detect_company_from_text("[KB금융]반기연결검토보고서(2025.08.14).pdf") == "KB"
    assert app.detect_company_from_text("KB금융지주 사업보고서.pdf") == "KB"


def test_company_detect_none():
    assert app.detect_company_from_text("output.pdf") is None
    assert app.detect_company_from_text("") is None


# ── 문서유형 감지 ────────────────────────────────────────────────────────────
def test_doctype_report():
    assert app.detect_doc_type_from_text("[신한지주]분기보고서(2025.11.14).pdf") == "report"
    assert app.detect_doc_type_from_text("[하나금융지주]반기보고서.pdf") == "report"
    assert app.detect_doc_type_from_text("[KB금융]사업보고서(2026.03.18).pdf") == "report"


def test_doctype_review_consolidated():
    assert app.detect_doc_type_from_text("[KB금융]반기연결검토보고서(2025.08.14).pdf") == "review"
    # 연결감사보고서(FY) → review
    assert app.detect_doc_type_from_text("[신한지주]연결감사보고서(2026.03.18).pdf") == "review"


def test_doctype_review_separate():
    # 검토/감사 & 연결 미포함 → 별도(review_sep)
    assert app.detect_doc_type_from_text("[신한지주]분기검토보고서(2026.05.15).pdf") == "review_sep"
    assert app.detect_doc_type_from_text("[KB금융]감사보고서(2026.03.18).pdf") == "review_sep"


def test_doctype_none():
    assert app.detect_doc_type_from_text("output.pdf") is None


# ── 기간 감지 ───────────────────────────────────────────────────────────────
def test_period_fy():
    assert app.detect_period_from_text("[신한지주]사업보고서(2026.03.18).pdf") == "2026FY"


def test_period_half_year():
    # 반기(접수 8월)는 분기보다 먼저 판정 → Q2
    assert app.detect_period_from_text("[KB금융]반기보고서(2025.08.14).pdf") == "2025Q2"


def test_period_quarter_by_month():
    # 분기 + 접수 5월 ≈ 1분기(Q1), 11월 ≈ 3분기(Q3)
    assert app.detect_period_from_text("[신한지주]분기보고서(2025.05.15).pdf") == "2025Q1"
    assert app.detect_period_from_text("[신한지주]분기보고서(2025.11.14).pdf") == "2025Q3"


def test_period_quarter_unknown_month_defaults_q3():
    # 분기인데 날짜 마커 없음 → 보수적 Q3 (단, 연도 없으면 None)
    assert app.detect_period_from_text("분기보고서(2025).pdf") == "2025Q3"
    assert app.detect_period_from_text("분기보고서.pdf") is None  # 연도 없음


def test_period_none():
    assert app.detect_period_from_text("output.pdf") is None


# ── 통합 감지 + 엣지 ─────────────────────────────────────────────────────────
def test_meta_full_filenames():
    m = app.detect_pdf_meta_from("[신한지주]분기보고서(2025.11.14).pdf")
    assert (m["company"], m["period"], m["doc_type"]) == ("신한", "2025Q3", "report")
    assert m["source"] == "filename"

    m = app.detect_pdf_meta_from("[KB금융]반기연결검토보고서(2025.08.14).pdf")
    assert (m["company"], m["period"], m["doc_type"]) == ("KB", "2025Q2", "review")


def test_meta_correction_prefix():
    # [정정]/[기재정정] 접두는 키워드 판정에 영향 없음
    m = app.detect_pdf_meta_from("[하나금융지주][정정]반기보고서(2025.08.14).pdf")
    assert (m["company"], m["period"], m["doc_type"]) == ("하나", "2025Q2", "report")


def test_meta_text_fallback_marks_source():
    # 파일명으로 부족 → 본문 텍스트 폴백 시 source 표기 변경
    m = app.detect_pdf_meta_from("output.pdf", page_text="[신한지주] 제5기 분기보고서 (2025.11.14)")
    assert m["company"] == "신한"
    assert m["doc_type"] == "report"
    assert m["source"] == "filename+text"


def test_meta_empty():
    m = app.detect_pdf_meta_from("output.pdf")
    assert m == {"company": None, "period": None, "doc_type": None, "source": "filename"}
