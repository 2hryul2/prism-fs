"""Build 2 — 검토보고서 연결/별도 picker·fetch 오프라인 단위테스트(네트워크 없음).

검증 범위:
- pick_review_doc_consolidated / pick_review_doc_separate 상호배타 분류(실측 title)
- 연결-only → separate None(오탐 0), 별도-only → consolidated None
- DataFrame(iterrows) 형태 입력도 흡수(_normalize_doc_rows 경유)
- fetch_review_sep_attachment 의 exclude_url 동일 시 None(중복 방지)

라이브 DART 호출 없음: attach_docs/attach_files 는 가짜 odr 로 주입, download_attachment 는 monkeypatch.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import collect_dart as cd  # noqa: E402


# --- 실측 title 픽스처 (STEP0 확인) -----------------------------------------
DOCS_BOTH = [
    {"title": "[신한지주]분기보고서(2025.11.14)", "url": "u_report"},
    {"title": "[신한지주]분기검토보고서(2025.11.14)", "url": "u_sep"},
    {"title": "[신한지주]분기연결검토보고서(2025.11.14)", "url": "u_conn"},
]
DOCS_CONN_ONLY = [
    {"title": "[KB금융]반기보고서(2025.08)", "url": "u_report"},
    {"title": "[KB금융]반기연결검토보고서(2025.08)", "url": "u_conn"},
]
DOCS_SEP_ONLY = [
    {"title": "[우리금융지주]반기보고서(2025.08)", "url": "u_report"},
    {"title": "[우리금융지주]반기검토보고서(2025.08)", "url": "u_sep"},
]


def test_both_attached_two_pickers_distinct():
    pc = cd.pick_review_doc_consolidated(DOCS_BOTH)
    ps = cd.pick_review_doc_separate(DOCS_BOTH)
    assert pc is not None and pc["url"] == "u_conn"
    assert ps is not None and ps["url"] == "u_sep"
    # 상호배타: 동일 행을 양쪽이 동시에 고르지 않음
    assert pc["url"] != ps["url"]


def test_consolidated_only_separate_none():
    """연결-only rows → separate picker None(오탐 0)."""
    assert cd.pick_review_doc_consolidated(DOCS_CONN_ONLY) is not None
    assert cd.pick_review_doc_separate(DOCS_CONN_ONLY) is None


def test_separate_only_consolidated_none():
    """별도-only(연결검토 부재) → consolidated None, separate 선택."""
    assert cd.pick_review_doc_consolidated(DOCS_SEP_ONLY) is None
    ps = cd.pick_review_doc_separate(DOCS_SEP_ONLY)
    assert ps is not None and ps["url"] == "u_sep"


def test_pickers_require_url():
    """url 빈 행은 후보가 될 수 없음."""
    rows = [{"title": "[X]분기연결검토보고서", "url": ""},
            {"title": "[X]분기검토보고서", "url": ""}]
    assert cd.pick_review_doc_consolidated(rows) is None
    assert cd.pick_review_doc_separate(rows) is None


def test_pickers_accept_dataframe_like():
    """attach_docs 가 DataFrame(iterrows)일 때도 _normalize_doc_rows 로 흡수."""
    class _FakeDF:
        def __init__(self, rows):
            self._rows = rows
            self.empty = not rows

        def iterrows(self):
            for i, r in enumerate(self._rows):
                yield i, r

    df = _FakeDF([
        {"title": "분기검토보고서", "url": "u_sep"},
        {"title": "분기연결검토보고서", "url": "u_conn"},
    ])
    assert cd.pick_review_doc_consolidated(df)["url"] == "u_conn"
    assert cd.pick_review_doc_separate(df)["url"] == "u_sep"


# --- fetch 레이어 (가짜 odr, 다운로드 monkeypatch — 네트워크 없음) -----------
class _FakeOdr:
    """attach_docs/attach_files 만 흉내내는 오프라인 odr 더블."""
    def __init__(self, files_by_url):
        self._files_by_url = files_by_url

    def attach_docs(self, rcept_no):  # prefetched_docs 로 우회하므로 실제론 미사용
        raise AssertionError("prefetched_docs 사용 시 attach_docs 호출 금지")

    def attach_files(self, url):
        return self._files_by_url.get(url, {})


def _patch_download_to_pdf(monkeypatch):
    """download_attachment 를 항상 성공(%PDF- 1바이트 기록)으로 대체."""
    def _fake_dl(url, dest_path):
        dest_path.write_bytes(b"%PDF-1.4 offline-fixture")
        return True
    monkeypatch.setattr(cd, "download_attachment", _fake_dl)


def test_review_sep_excluded_when_same_url(monkeypatch, tmp_path):
    """exclude_url 가 연결 review 첨부와 동일하면 review_sep None(중복 방지)."""
    _patch_download_to_pdf(monkeypatch)
    # 연결 후보 url 과 별도 후보 url 의 attach_files 가 같은 pdf_url 을 가리키는 케이스
    shared_pdf = "https://dart/pdf.do?same"
    odr = _FakeOdr({
        "u_conn": {"[신한]분기연결검토보고서.pdf": shared_pdf},
        "u_sep": {"[신한]분기검토보고서.pdf": shared_pdf},
    })
    review = cd.fetch_review_attachment(odr, "20250101", tmp_path,
                                        prefetched_docs=DOCS_BOTH)
    assert review is not None and review["file"] == "review.pdf"
    assert review["pdf_url"] == shared_pdf
    sep = cd.fetch_review_sep_attachment(odr, "20250101", tmp_path,
                                         prefetched_docs=DOCS_BOTH,
                                         exclude_url=review["pdf_url"])
    assert sep is None  # 동일 URL → 중복 스킵


def test_review_sep_collected_when_distinct_url(monkeypatch, tmp_path):
    """연결/별도 첨부 URL 이 다르면 양쪽 모두 수집, 표준본 파일명 분리."""
    _patch_download_to_pdf(monkeypatch)
    odr = _FakeOdr({
        "u_conn": {"[신한]분기연결검토보고서.pdf": "https://dart/pdf.do?conn"},
        "u_sep": {"[신한]분기검토보고서.pdf": "https://dart/pdf.do?sep"},
    })
    review = cd.fetch_review_attachment(odr, "20250101", tmp_path,
                                        prefetched_docs=DOCS_BOTH)
    sep = cd.fetch_review_sep_attachment(odr, "20250101", tmp_path,
                                         prefetched_docs=DOCS_BOTH,
                                         exclude_url=review["pdf_url"])
    assert review["file"] == "review.pdf" and review["doc_type"] == "review"
    assert sep is not None and sep["file"] == "review_sep.pdf"
    assert sep["doc_type"] == "review_sep"
    assert sep["filename_original"] == "[신한]분기검토보고서.pdf"
    assert (tmp_path / "review.pdf").exists()
    assert (tmp_path / "review_sep.pdf").exists()


def test_review_sep_none_when_no_separate(monkeypatch, tmp_path):
    """별도 후보가 없으면(연결-only) review_sep None — 표준본 미생성."""
    _patch_download_to_pdf(monkeypatch)
    odr = _FakeOdr({"u_conn": {"[KB]반기연결검토보고서.pdf": "https://dart/pdf.do?conn"}})
    sep = cd.fetch_review_sep_attachment(odr, "20250101", tmp_path,
                                         prefetched_docs=DOCS_CONN_ONLY)
    assert sep is None
    assert not (tmp_path / "review_sep.pdf").exists()
