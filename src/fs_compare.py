"""
fs_compare.py — 재무제표 결정론 비교 엔진 (prism-fs)

fs_structured.json(by_fs_div: CFS/OFS)의 **원문 금액**만으로 증감·연결vs별도·벤치마킹·
구조비율을 계산한다. 모든 파생값에 계산식·입력 원문(provenance)을 동봉한다.

안전경계(불변):
- AI/LLM 무경유. 단위 환산 없음. 원문 문자열 보존.
- 금액 연산은 Python int(임의정밀도)로 수행 → 부동소수 오차 0.
- 비교 컬럼은 sj_div 별 자동 선택(BS=전기말 frmtrm, 손익=전기동기 frmtrm_q).
- bfefrmtrm(전전기)은 사실상 비어 있어 단일 셀 추론 금지(시계열은 여러 기간 셀 교차 로드).
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import safety  # provenance 표준 형태 중앙화

BASE_DIR = Path(__file__).resolve().parent
LIBRARY_ROOT = BASE_DIR / "storage" / "library"

COMPANIES = ["신한", "KB", "하나", "우리"]
FSDIV_TO_KEY = {"연결": "CFS", "별도": "OFS"}
FSDIV_LABEL = {"CFS": "연결(CFS)", "OFS": "별도(OFS)"}

# 비교 컬럼 자동 선택: 재무상태표=전기말, 손익/현금흐름=전기동기.
CMP_COL = {
    "BS":  ("frmtrm", "전기말"),
    "CIS": ("frmtrm_q", "전기동기"),
    "IS":  ("frmtrm_q", "전기동기"),
    "CF":  ("frmtrm_q", "전기동기"),
    "SCE": ("frmtrm", "전기"),
}


def _to_int(s: Any) -> Optional[int]:
    """원문 금액 문자열 → int(임의정밀도). 빈 값/'-'/None 은 None. 환산 아님."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-"):
        return None
    neg = t.startswith("-")
    digits = "".join(ch for ch in t if ch.isdigit())
    if not digits:
        return None
    v = int(digits)
    return -v if neg else v


def load_fs(company: str, period: str) -> Optional[dict]:
    """fs_structured.json 로드. 없으면 None."""
    fp = LIBRARY_ROOT / company / period / "fs_structured.json"
    if not fp.exists():
        return None
    try:
        return json.load(io.open(fp, encoding="utf-8"))
    except Exception:
        return None


def _accounts(company: str, period: str, fs_key: str) -> List[dict]:
    d = load_fs(company, period)
    if not d:
        return []
    block = (d.get("by_fs_div") or {}).get(fs_key) or {}
    return block.get("accounts", []) if isinstance(block, dict) else []


def _find(accounts: List[dict], account_id: str) -> Optional[dict]:
    return next((a for a in accounts if a.get("account_id") == account_id), None)


def list_periods(company: str) -> List[str]:
    """해당 회사의 fs_structured.json 보유 기간 목록(정렬). 시계열용."""
    base = LIBRARY_ROOT / company
    if not base.is_dir():
        return []
    out = [p.name for p in base.iterdir()
           if p.is_dir() and (p / "fs_structured.json").exists()]
    return sorted(out)  # "2025Q2" < "2025Q3" < "2026Q1" 사전식=시간순


def list_accounts(company: str, period: str, fs_div: str = "연결") -> List[dict]:
    """해당 셀의 계정 목록(원문 필드 보존)."""
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    out = []
    for a in _accounts(company, period, fs_key):
        out.append({
            "account_id": a.get("account_id"),
            "account_nm": a.get("account_nm"),
            "sj_div": a.get("sj_div"),
            "thstrm": a.get("thstrm_amount"),
            "frmtrm": a.get("frmtrm_amount"),
            "frmtrm_q": a.get("frmtrm_q_amount"),
            "currency": a.get("currency"),
        })
    return out


def delta(company: str, period: str, fs_div: str = "연결") -> Dict[str, Any]:
    """계정별 당기 vs 전기(비교컬럼 자동선택) 증감액·증감률 + provenance."""
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    rows = []
    for a in _accounts(company, period, fs_key):
        sj = a.get("sj_div")
        col, col_ko = CMP_COL.get(sj, ("frmtrm", "전기"))
        cur = _to_int(a.get("thstrm_amount"))
        # 비교 컬럼 원문값: 손익(frmtrm_q)=전기동기, 그 외(frmtrm)=전기말
        base_raw = a.get("frmtrm_q_amount") if col == "frmtrm_q" else a.get("frmtrm_amount")
        base = _to_int(base_raw)
        row = {
            "account_id": a.get("account_id"),
            "account_nm": a.get("account_nm"),
            "sj_div": sj,
            "thstrm": a.get("thstrm_amount"),
            "compare_col": col, "compare_col_ko": col_ko,
            "base": base_raw,
        }
        if cur is None or base is None:
            row["delta"] = None
            row["pct"] = None
            row["note"] = "N/A" if base is None else None
        else:
            dv = cur - base
            row["delta"] = str(dv)
            row["pct"] = "N/A" if base == 0 else round(dv / abs(base) * 100, 2)
            row["provenance"] = safety.provenance(
                formula=f"Δ = 당기 − {col_ko} ; 증감률 = Δ / |{col_ko}| × 100",
                inputs=[
                    {"label": "당기(thstrm)", "raw": a.get("thstrm_amount"),
                     "account_id": a.get("account_id"), "period": period, "fs_div": fs_div},
                    {"label": f"{col_ko}({col})", "raw": base_raw,
                     "account_id": a.get("account_id"), "period": period, "fs_div": fs_div},
                ],
                result=f"Δ={dv} , 증감률={row['pct']}",
            )
        rows.append(row)
    return {"company": company, "period": period, "fs_div": fs_div, "rows": rows}


def consolidated_vs_separate(company: str, period: str) -> Dict[str, Any]:
    """동일 회사·기간 account_id 의 연결(CFS) − 별도(OFS) 차이 = 자회사효과(추정)."""
    cfs = _accounts(company, period, "CFS")
    ofs = _accounts(company, period, "OFS")
    ids = list(dict.fromkeys([a.get("account_id") for a in cfs] + [a.get("account_id") for a in ofs]))
    rows = []
    for aid in ids:
        ca, oa = _find(cfs, aid), _find(ofs, aid)
        nm = (ca or oa or {}).get("account_nm")
        row = {"account_id": aid, "account_nm": nm,
               "cfs": ca.get("thstrm_amount") if ca else None,
               "ofs": oa.get("thstrm_amount") if oa else None}
        cv, ov = _to_int(row["cfs"]), _to_int(row["ofs"])
        if ca and oa and cv is not None and ov is not None:
            dv = cv - ov
            row["diff"] = str(dv)
            row["provenance"] = safety.provenance(
                formula="차이 = 연결(CFS) − 별도(OFS)  [자회사효과 추정·연결조정 미반영]",
                inputs=[
                    {"label": "연결 당기", "raw": row["cfs"], "account_id": aid, "period": period, "fs_div": "연결"},
                    {"label": "별도 당기", "raw": row["ofs"], "account_id": aid, "period": period, "fs_div": "별도"},
                ],
                result=str(dv),
            )
        else:
            row["diff"] = None
            row["flag"] = "대응 없음"  # 한쪽만 존재 → 차감 안 함
        rows.append(row)
    return {"company": company, "period": period, "rows": rows}


def benchmark(period: str, account_id: str, fs_div: str = "연결") -> Dict[str, Any]:
    """동일 기간·fs_div·account_id 4사 비교 + 순위 + 단위 경고."""
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    present, missing, units = [], [], set()
    for c in COMPANIES:
        a = _find(_accounts(c, period, fs_key), account_id)
        if a and _to_int(a.get("thstrm_amount")) is not None:
            present.append({"company": c, "thstrm": a.get("thstrm_amount"),
                            "account_nm": a.get("account_nm"), "currency": a.get("currency")})
            if a.get("currency"):
                units.add(a.get("currency"))
        else:
            missing.append(c)
    # 순위(내림차순) — 표시/순위만, 값 불변
    order = sorted(present, key=lambda r: _to_int(r["thstrm"]), reverse=True)
    rank = {r["company"]: i + 1 for i, r in enumerate(order)}
    for r in present:
        r["rank"] = rank[r["company"]]
    return {
        "period": period, "fs_div": fs_div, "account_id": account_id,
        "account_nm": present[0]["account_nm"] if present else account_id,
        "rows": present, "missing": missing,
        "unit_warning": len(units) > 1, "units_seen": sorted(units),
    }


def ratio(company: str, period: str, fs_div: str = "연결") -> Dict[str, Any]:
    """안전 구조비율 — 분자·분모가 모두 원문 존재 시에만 계산."""
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    accs = _accounts(company, period, fs_key)
    A = _find(accs, "ifrs-full_Assets")
    L = _find(accs, "ifrs-full_Liabilities")
    E = _find(accs, "ifrs-full_Equity")

    def _ratio_row(label: str, num: Optional[dict], den: Optional[dict], num_ko: str, den_ko: str):
        if not num or not den:
            return {"label": label, "value": None, "reason": "분자/분모 원문 미존재"}
        nv, dv = _to_int(num.get("thstrm_amount")), _to_int(den.get("thstrm_amount"))
        if nv is None or dv is None:
            return {"label": label, "value": None, "reason": "원문 값 없음"}
        if dv == 0:
            return {"label": label, "value": "N/A", "reason": "분모 0"}
        val = round(nv / dv * 100, 2)
        return {
            "label": label, "value": val,
            "provenance": safety.provenance(
                formula=f"{label} = {num_ko} ÷ {den_ko} × 100",
                inputs=[
                    {"label": num_ko, "raw": num.get("thstrm_amount"),
                     "account_id": num.get("account_id"), "period": period, "fs_div": fs_div},
                    {"label": den_ko, "raw": den.get("thstrm_amount"),
                     "account_id": den.get("account_id"), "period": period, "fs_div": fs_div},
                ],
                result=f"{val}%",
            ),
        }

    return {
        "company": company, "period": period, "fs_div": fs_div,
        "rows": [
            _ratio_row("부채비율", L, E, "부채총계", "자본총계"),
            _ratio_row("자기자본비율", E, A, "자본총계", "자산총계"),
        ],
    }


# 시계열 추이 대상 핵심 계정(고정 — 결정론). 회사·기간 무관 IFRS 표준 account_id.
TIMESERIES_ACCOUNTS = [
    ("ifrs-full_Assets", "자산총계"),
    ("ifrs-full_Liabilities", "부채총계"),
    ("ifrs-full_Equity", "자본총계"),
    ("ifrs-full_ProfitLossFromOperatingActivities", "영업이익"),
    ("ifrs-full_ProfitLoss", "분기순이익"),
]


def timeseries(company: str, account_id: str, fs_div: str = "연결") -> Dict[str, Any]:
    """여러 기간 셀에 걸친 당기(thstrm) 원문 추이 + 인접 기간 Δ/%(결정론).

    안전경계: 각 기간의 당기 원문만 나열·인접 비교. 단위 환산·추론 없음.
    """
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    periods = list_periods(company)
    points = []
    acc_nm = account_id
    for p in periods:
        a = _find(_accounts(company, p, fs_key), account_id)
        if a:
            acc_nm = a.get("account_nm") or acc_nm
        points.append({"period": p, "value": a.get("thstrm_amount") if a else None})
    # 인접 기간 증감(결정론)
    rows = []
    for i, pt in enumerate(points):
        cur = _to_int(pt["value"])
        prev = _to_int(points[i - 1]["value"]) if i > 0 else None
        row = {"period": pt["period"], "value": pt["value"], "delta": None, "pct": None}
        if cur is not None and prev is not None:
            dv = cur - prev
            row["delta"] = str(dv)
            row["pct"] = "N/A" if prev == 0 else round(dv / abs(prev) * 100, 2)
            row["provenance"] = safety.provenance(
                formula=f"Δ = {pt['period']} − {points[i-1]['period']} (당기 원문)",
                inputs=[
                    {"label": pt["period"], "raw": pt["value"], "account_id": account_id, "period": pt["period"], "fs_div": fs_div},
                    {"label": points[i-1]["period"], "raw": points[i-1]["value"], "account_id": account_id, "period": points[i-1]["period"], "fs_div": fs_div},
                ],
                result=f"Δ={dv} , 증감률={row['pct']}",
            )
        rows.append(row)
    return {"company": company, "account_id": account_id, "account_nm": acc_nm,
            "fs_div": fs_div, "rows": rows}


# 이상치 임계(전기 대비 |증감률|). 결정론 — 단순 룰, 판단 아님.
FLAG_PCT_THRESHOLD = 50.0


def flags(company: str, period: str, fs_div: str = "연결") -> Dict[str, Any]:
    """결정론 이상치 플래그(전기 대비). '확인 필요' 톤 — 판단/생성 없음.

    룰: ① 부호 반전(당기·전기 부호 다름) ② |증감률|≥임계.
    (연결/별도 계정 수 차이는 구조적 정상이라 '대응 없음'은 플래그하지 않음 — 노이즈 방지.)
    """
    fs_key = FSDIV_TO_KEY.get(fs_div, "CFS")
    accs = _accounts(company, period, fs_key)
    out = []
    for a in accs:
        sj = a.get("sj_div")
        col = "frmtrm_q_amount" if (CMP_COL.get(sj, ("frmtrm",))[0] == "frmtrm_q") else "frmtrm_amount"
        cur, prev = _to_int(a.get("thstrm_amount")), _to_int(a.get(col))
        reasons = []
        if cur is not None and prev is not None and prev != 0:
            if (cur < 0) != (prev < 0):
                reasons.append("부호 반전(전기↔당기)")
            pct = abs((cur - prev) / abs(prev) * 100)
            if pct >= FLAG_PCT_THRESHOLD:
                reasons.append(f"급변 {round(pct,1)}% (≥{FLAG_PCT_THRESHOLD}%)")
        if reasons:
            out.append({"account_id": a.get("account_id"), "account_nm": a.get("account_nm"),
                        "sj_div": sj, "thstrm": a.get("thstrm_amount"),
                        "compare": a.get(col), "reasons": reasons})
    return {"company": company, "period": period, "fs_div": fs_div,
            "threshold_pct": FLAG_PCT_THRESHOLD, "flagged": out}


def consolidated_subtotals(company: str, period: str) -> Dict[str, Any]:
    """연결조정(자회사효과) sj_div 소계 상세 — 자산/부채/자본·손익 핵심계정 CFS−OFS + 비중(추정)."""
    KEY = [("ifrs-full_Assets", "자산총계", "BS"),
           ("ifrs-full_Liabilities", "부채총계", "BS"),
           ("ifrs-full_Equity", "자본총계", "BS"),
           ("ifrs-full_ProfitLossFromOperatingActivities", "영업이익", "CIS"),
           ("ifrs-full_ProfitLoss", "분기순이익", "CIS")]
    cfs, ofs = _accounts(company, period, "CFS"), _accounts(company, period, "OFS")
    rows = []
    for aid, nm, sj in KEY:
        ca, oa = _find(cfs, aid), _find(ofs, aid)
        cv, ov = (_to_int(ca.get("thstrm_amount")) if ca else None), (_to_int(oa.get("thstrm_amount")) if oa else None)
        row = {"account_id": aid, "account_nm": nm, "sj_div": sj,
               "cfs": ca.get("thstrm_amount") if ca else None,
               "ofs": oa.get("thstrm_amount") if oa else None}
        if cv is not None and ov is not None:
            dv = cv - ov
            row["diff"] = str(dv)
            row["pct_of_cfs"] = "N/A" if cv == 0 else round(dv / abs(cv) * 100, 1)  # 자회사효과 비중(연결 대비)
            row["provenance"] = safety.provenance(
                formula="자회사효과(추정) = 연결 − 별도 ; 비중 = 차이 / |연결| × 100",
                inputs=[
                    {"label": "연결", "raw": row["cfs"], "account_id": aid, "period": period, "fs_div": "연결"},
                    {"label": "별도", "raw": row["ofs"], "account_id": aid, "period": period, "fs_div": "별도"},
                ],
                result=f"차이={dv} , 비중={row['pct_of_cfs']}%",
            )
        else:
            row["diff"] = None
            row["flag"] = "대응 없음"
        rows.append(row)
    return {"company": company, "period": period, "rows": rows, "estimated": True}
