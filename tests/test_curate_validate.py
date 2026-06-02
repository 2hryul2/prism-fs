"""curate_validate 순수 함수 단위 테스트(app import 불요, 주입형 page_text_provider).

검증 대상: verbatim 통과/실패, 숫자성 제목 거부, 페이지 경계 위반, fs_div 오류, 단조 경고.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from curate_index_claude import curate_validate  # noqa: E402


def _provider_from_pages(pages: dict):
    """페이지번호(1-based)->텍스트 매핑으로 (p_start,p_end)->합본 텍스트 provider 구성."""
    def provider(p_start, p_end):
        return "\n".join(pages.get(p, "") for p in range(p_start, p_end + 1))
    return provider


def _note(no, title, ps, pe, fs_div="연결"):
    return {"no": no, "title": title, "page_start": ps, "page_end": pe, "fs_div": fs_div}


def test_verbatim_pass():
    pages = {1: "1. 회사의 개요\n당사는 ...", 2: "2. 중요한 회계정책"}
    notes = [
        _note(1, "회사의 개요", 1, 1),
        _note(2, "중요한 회계정책", 2, 2),
    ]
    violations, warnings = curate_validate(
        notes, total_pages=2, page_text_provider=_provider_from_pages(pages)
    )
    assert violations == [], violations
    assert warnings == [], warnings


def test_verbatim_pass_ignores_whitespace():
    # 본문 레이아웃 공백/줄바꿈 차이는 정규화로 흡수되어야 함
    pages = {1: "회 사 의\n개요"}
    notes = [_note(1, "회사의개요", 1, 1)]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert violations == [], violations
    assert warnings == [], warnings


def test_verbatim_fail():
    pages = {1: "전혀 다른 본문 내용"}
    notes = [_note(1, "존재하지않는제목", 1, 1)]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert any("verbatim" in m for m in violations), violations


def test_numeric_title_rejected():
    pages = {1: "1,234,567"}
    notes = [_note(1, "1,234,567", 1, 1)]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert any("숫자성 제목" in m for m in violations), violations


def test_page_bound_violation():
    pages = {1: "회사의 개요"}
    # page_end(5) > total_pages(1) → 경계 위반
    notes = [_note(1, "회사의 개요", 1, 5)]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert any("페이지 경계 위반" in m for m in violations), violations


def test_page_bound_start_below_one():
    pages = {1: "회사의 개요"}
    notes = [_note(1, "회사의 개요", 0, 1)]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert any("페이지 경계 위반" in m for m in violations), violations


def test_bad_fs_div():
    pages = {1: "회사의 개요"}
    notes = [_note(1, "회사의 개요", 1, 1, fs_div="결합")]
    violations, warnings = curate_validate(notes, 1, _provider_from_pages(pages))
    assert any("fs_div 오류" in m for m in violations), violations


def test_monotonic_warning():
    # 같은 fs_div(연결) 내 page_start 역행 → 경고(미차단)
    pages = {1: "회사의 개요", 5: "중요한 회계정책", 3: "현금및현금성자산"}
    notes = [
        _note(1, "회사의 개요", 1, 1),
        _note(2, "중요한 회계정책", 5, 5),
        _note(3, "현금및현금성자산", 3, 3),  # 5 → 3 역행
    ]
    violations, warnings = curate_validate(notes, 5, _provider_from_pages(pages))
    # 역행은 경고 채널에만 — 위반에는 없어야 함
    assert any("역행" in m for m in warnings), warnings
    assert violations == [], violations


def test_monotonic_ok_across_different_div():
    # 별도 섹션은 페이지가 다시 앞으로 가도 정상(div 별 독립 추적)
    pages = {1: "회사의 개요", 5: "중요한 회계정책", 2: "별도 회사의 개요"}
    notes = [
        _note(1, "회사의 개요", 1, 1, fs_div="연결"),
        _note(2, "중요한 회계정책", 5, 5, fs_div="연결"),
        _note(1, "별도 회사의 개요", 2, 2, fs_div="별도"),
    ]
    violations, warnings = curate_validate(notes, 5, _provider_from_pages(pages))
    assert violations == [], violations
    assert warnings == [], warnings


def test_duplicate_no_fsdiv_warning():
    pages = {1: "회사의 개요", 2: "회사의 개요 둘"}
    notes = [
        _note(1, "회사의 개요", 1, 1, fs_div="연결"),
        _note(1, "회사의 개요 둘", 2, 2, fs_div="연결"),  # (no=1, 연결) 중복
    ]
    violations, warnings = curate_validate(notes, 2, _provider_from_pages(pages))
    # 중복은 경고 채널에만 — 위반에는 없어야 함
    assert any("중복" in m for m in warnings), warnings
    assert violations == [], violations
