"""safety.py 중앙화 검증 — 시크릿 마스킹·provenance 표준형태."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import safety  # noqa: E402


def test_mask_dart_key():
    s = safety.mask_secrets("https://opendart.fss.or.kr/api?crtfc_key=0123456789abcdef&x=1")
    assert "0123456789abcdef" not in s
    assert "crtfc_key=****" in s


def test_mask_openai_and_bearer():
    s = safety.mask_secrets("OPENAI_API_KEY: sk-proj-ABCDEFGHIJ1234567890")
    assert "sk-proj-ABCDEFGHIJ" not in s
    assert "****" in s
    b = safety.mask_secrets("Authorization: Bearer sk-abcdefghij1234567890")
    assert "sk-abcdefghij" not in b


def test_safe_err_masks_and_caps():
    e = ValueError("crtfc_key=SECRETKEY123456 실패")
    out = safety.safe_err(e)
    assert "SECRETKEY123456" not in out
    assert out.startswith("ValueError")
    assert len(out) <= 300


def test_provenance_standard_shape():
    p = safety.provenance(
        formula="Δ = 당기 − 전기",
        inputs=[{"label": "당기", "raw": "100", "account_id": "X", "period": "2026Q1", "fs_div": "연결"}],
        result="Δ=10",
    )
    assert p["engine"] == "deterministic"  # AI 무경유 보증 표식
    assert p["formula"] == "Δ = 당기 − 전기"
    assert p["result"] == "Δ=10"
    assert len(p["inputs"]) == 1
