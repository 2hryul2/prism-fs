"""static/index.html 에 외부 CDN(https 호스트) 참조가 없는지 검증 — 폐쇄망 안전 게이트."""
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "static" / "index.html"

# 표준 네임스페이스(URL 식별자, 실제 fetch 아님)는 허용.
ALLOW = ("www.w3.org", "json-schema.org")


def test_no_external_https_hosts():
    html = io.open(INDEX, encoding="utf-8").read()
    hosts = re.findall(r"https?://([^/\"'\s)]+)", html)
    external = [h for h in hosts if not any(a in h for a in ALLOW)]
    assert not external, f"외부 호스트 참조 발견(폐쇄망 위반): {sorted(set(external))}"


def test_pdfjs_is_vendored():
    html = io.open(INDEX, encoding="utf-8").read()
    assert "vendor/pdfjs/pdf.min.mjs" in html, "pdf.js 가 vendored 경로가 아님"
    assert (ROOT / "src" / "static" / "vendor" / "pdfjs" / "pdf.min.mjs").exists()
    assert (ROOT / "src" / "static" / "vendor" / "pdfjs" / "pdf.worker.min.mjs").exists()
