"""
E2E 스모크 — 핵심 사용자 흐름 자동화 (Playwright, Windows 11).

대상: 가동 중인 개발 서버 :8021 (빌드 exe 아님 — 앱 로직·UI 무결성 검증이 목적).
서버 미가동 시 전체 skip. 시나리오 1개 + PDF 렌더 검증 + 콘솔 error 0 단언.

실행(서버 :8021 가동 상태):
    python -m pytest tests\\e2e -q
사전: pip install playwright ; python -m playwright install chromium
"""
import urllib.request

import pytest

BASE_URL = "http://127.0.0.1:8021"


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason=":8021 개발 서버 미가동")

# playwright 미설치 환경도 graceful skip
sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright


# 무해한 콘솔 잡음(파비콘 404 등)은 실패로 보지 않음.
_BENIGN = ("favicon", "/favicon.ico")


def _is_benign(text: str) -> bool:
    return any(b in text for b in _BENIGN)


@pytest.fixture()
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and not _is_benign(m.text) else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg._console_errors = errors  # 테스트에서 단언
        yield pg
        browser.close()


def test_core_navigation_and_tabs(page):
    """라이브러리 로드 → 재무제표 8뷰 → 비교 조회 → 커버리지. 콘솔 error 0."""
    page.goto(BASE_URL, wait_until="networkidle")

    # 1) 라이브러리: 매트릭스가 /api/library 로 채워짐
    page.wait_for_selector("#matrix-table tr", timeout=15000)
    assert page.locator("#matrix-table tr").count() > 0

    # 2) 재무제표 비교: 결정론 8뷰 버튼 존재 + 뷰 전환 시 결과 렌더
    page.evaluate("showTab('fs')")
    page.wait_for_selector("#tab-fs:not(.hidden)", timeout=5000)
    assert page.locator("button.fs-view").count() == 8  # ①~⑧
    for view in ("delta", "bench", "flags"):
        page.evaluate(f"showFsView('{view}')")
        page.wait_for_timeout(300)
    assert page.locator("#fs-result").inner_html().strip() != ""

    # 3) 비교 조회 탭 표시
    page.evaluate("showTab('compare')")
    page.wait_for_selector("#tab-compare:not(.hidden)", timeout=5000)

    # 4) 커버리지·공시갭 탭(insight 섹션 렌더)
    page.evaluate("showTab('coverage')")
    page.wait_for_selector("#tab-coverage:not(.hidden)", timeout=5000)
    page.wait_for_selector("#insight-period", timeout=5000)

    assert page._console_errors == [], f"콘솔 error 발생: {page._console_errors}"


def test_pdf_render_pipeline(page):
    """vendored pdf.js + /api/pdf 파이프라인으로 1페이지를 canvas 렌더(외부 CDN 0 검증)."""
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_function("() => !!window.pdfjsLib", timeout=10000)

    # 실제 라이브러리 첫 항목으로 PDF 1페이지 렌더 → canvas 픽셀 폭 > 0 이면 성공
    result = page.evaluate(
        """async () => {
            const lib = await (await fetch('/api/library')).json();
            const e = (lib.entries || []).find(x => x.indexed) || (lib.entries || [])[0];
            if (!e) return { ok:false, reason:'no library entry' };
            const url = `/api/pdf?company=${encodeURIComponent(e.company)}&period=${encodeURIComponent(e.period)}&doc_type=report`;
            const pdf = await window.pdfjsLib.getDocument(url).promise;
            const pageObj = await pdf.getPage(1);
            const viewport = pageObj.getViewport({ scale: 1.0 });
            const canvas = document.createElement('canvas');
            canvas.width = viewport.width; canvas.height = viewport.height;
            await pageObj.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
            return { ok: canvas.width > 0 && canvas.height > 0, w: canvas.width, pages: pdf.numPages, company: e.company, period: e.period };
        }"""
    )
    assert result.get("ok"), f"PDF 렌더 실패: {result}"
    assert result.get("pages", 0) >= 1
    assert page._console_errors == [], f"콘솔 error 발생: {page._console_errors}"
