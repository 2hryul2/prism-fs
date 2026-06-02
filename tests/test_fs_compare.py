"""fs_compare 결정론 엔진 안전경계 검증 — provenance·분모0·대응없음·원문 일치·비교컬럼."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import fs_compare  # noqa: E402

LIB = ROOT / "src" / "storage" / "library"
HAS_DATA = (LIB / "신한" / "2026Q1" / "fs_structured.json").exists()
import pytest  # noqa: E402
skip_no_data = pytest.mark.skipif(not HAS_DATA, reason="신한 2026Q1 fs_structured.json 미수집")


def test_cmp_col_fy_uses_annual_prior():
    # 분기: 손익=전기동기(frmtrm_q), 재무상태표=전기말(frmtrm)
    assert fs_compare.cmp_col("CIS", "2026Q1")[0] == "frmtrm_q"
    assert fs_compare.cmp_col("BS", "2026Q1")[0] == "frmtrm"
    # 연간(FY): 손익은 전기 연간(frmtrm) 비교, BS 는 그대로 frmtrm
    assert fs_compare.cmp_col("CIS", "2025FY")[0] == "frmtrm"
    assert fs_compare.cmp_col("IS", "2025FY")[0] == "frmtrm"
    assert fs_compare.cmp_col("BS", "2025FY")[0] == "frmtrm"


def test_to_int_preserves_sign_and_bigint():
    assert fs_compare._to_int("816718546000000") == 816718546000000
    assert fs_compare._to_int("-46715000000") == -46715000000
    assert fs_compare._to_int("") is None
    assert fs_compare._to_int("-") is None


@skip_no_data
def test_delta_matches_raw_and_column_autoselect():
    d = fs_compare.delta("신한", "2026Q1", "연결")
    rows = {r["account_id"]: r for r in d["rows"]}
    asset = rows["ifrs-full_Assets"]
    # BS → 전기말(frmtrm), 원문 그대로 차감
    assert asset["compare_col"] == "frmtrm"
    raw = json.load(io.open(LIB / "신한" / "2026Q1" / "fs_structured.json", encoding="utf-8"))
    a = next(x for x in raw["by_fs_div"]["CFS"]["accounts"]
             if x["account_id"] == "ifrs-full_Assets" and x["sj_div"] == "BS")
    assert asset["thstrm"] == a["thstrm_amount"]  # 원문 보존(바이트 동일)
    assert int(asset["delta"]) == int(a["thstrm_amount"]) - int(a["frmtrm_amount"])
    # 손익 → 전기동기(frmtrm_q)
    op = rows["ifrs-full_ProfitLossFromOperatingActivities"]
    assert op["compare_col"] == "frmtrm_q"
    # provenance 필수 필드
    assert asset["provenance"]["engine"] == "deterministic"
    assert len(asset["provenance"]["inputs"]) == 2


@skip_no_data
def test_cons_vs_sep_diff_and_provenance():
    d = fs_compare.consolidated_vs_separate("신한", "2026Q1")
    asset = next(r for r in d["rows"] if r["account_id"] == "ifrs-full_Assets")
    assert int(asset["diff"]) == int(asset["cfs"]) - int(asset["ofs"])
    assert asset["provenance"]["formula"].startswith("차이 = 연결")


@skip_no_data
def test_ratio_denominator_and_provenance():
    d = fs_compare.ratio("신한", "2026Q1", "연결")
    labels = {r["label"]: r for r in d["rows"]}
    assert "부채비율" in labels and "자기자본비율" in labels
    for r in d["rows"]:
        if r.get("value") not in (None, "N/A"):
            assert r["provenance"]["engine"] == "deterministic"


def test_ratio_zero_denominator_is_na(monkeypatch):
    # 분모 0 → N/A (계산하지 않음)
    fake = [
        {"account_id": "ifrs-full_Liabilities", "account_nm": "부채총계", "sj_div": "BS", "thstrm_amount": "100"},
        {"account_id": "ifrs-full_Equity", "account_nm": "자본총계", "sj_div": "BS", "thstrm_amount": "0"},
        {"account_id": "ifrs-full_Assets", "account_nm": "자산총계", "sj_div": "BS", "thstrm_amount": "100"},
    ]
    monkeypatch.setattr(fs_compare, "_accounts", lambda c, p, k: fake)
    d = fs_compare.ratio("X", "Y", "연결")
    debt = next(r for r in d["rows"] if r["label"] == "부채비율")
    assert debt["value"] == "N/A"  # 부채/자본, 자본=0 → N/A


def test_cons_vs_sep_missing_side_flagged(monkeypatch):
    # 한쪽만 존재 → diff None + 대응없음 플래그(차감 안 함)
    def fake_accounts(c, p, k):
        if k == "CFS":
            return [{"account_id": "X1", "account_nm": "테스트", "sj_div": "BS", "thstrm_amount": "500"}]
        return []  # OFS 비어있음
    monkeypatch.setattr(fs_compare, "_accounts", fake_accounts)
    d = fs_compare.consolidated_vs_separate("C", "P")
    r = d["rows"][0]
    assert r["diff"] is None and r.get("flag") == "대응 없음"


# ---- P3: 시계열·이상치·연결조정 상세 ----
@skip_no_data
def test_timeseries_multi_period_and_provenance():
    d = fs_compare.timeseries("신한", "ifrs-full_Assets", "연결")
    assert len(d["rows"]) >= 2  # 여러 기간 교차 로드
    # 첫 기간은 delta None, 이후는 인접 비교 provenance 동봉
    later = [r for r in d["rows"] if r.get("delta") is not None]
    assert later and later[0]["provenance"]["engine"] == "deterministic"
    # 원문 보존(가공 0)
    for r in d["rows"]:
        if r["value"] is not None:
            assert isinstance(r["value"], str)


@skip_no_data
def test_flags_no_correspondence_noise_removed():
    d = fs_compare.flags("신한", "2026Q1", "연결")
    # '대응 없음'은 플래그하지 않음(구조적 정상) — 노이즈 제거
    for f in d["flagged"]:
        assert all("대응 없음" not in r for r in f["reasons"])
        assert all(("부호 반전" in r or "급변" in r) for r in f["reasons"])


@skip_no_data
def test_consolidated_subtotals_diff_and_estimated():
    d = fs_compare.consolidated_subtotals("신한", "2026Q1")
    assert d["estimated"] is True
    asset = next(r for r in d["rows"] if r["account_id"] == "ifrs-full_Assets")
    assert int(asset["diff"]) == int(asset["cfs"]) - int(asset["ofs"])
    assert asset["provenance"]["engine"] == "deterministic"


def test_flags_threshold_rule(monkeypatch):
    fake = [
        {"account_id": "A", "account_nm": "급변계정", "sj_div": "BS", "thstrm_amount": "300", "frmtrm_amount": "100"},
        {"account_id": "B", "account_nm": "안정계정", "sj_div": "BS", "thstrm_amount": "105", "frmtrm_amount": "100"},
        {"account_id": "C", "account_nm": "부호반전", "sj_div": "CIS", "thstrm_amount": "-50", "frmtrm_q_amount": "50"},
    ]
    monkeypatch.setattr(fs_compare, "_accounts", lambda c, p, k: fake)
    d = fs_compare.flags("X", "Y", "연결")
    flagged = {f["account_id"] for f in d["flagged"]}
    assert "A" in flagged and "C" in flagged and "B" not in flagged  # 200% 급변, 부호반전 / 5%는 정상
