"""xbrl_tagging 순수 함수 단위 테스트(XBRL 파일 불요, 합성 입력).

검증: roleType definition 파싱·fs_div 규칙·href→QName·L1 차집합·L3 매핑(fs_div 한정·형제롤 dedup).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import xbrl_tagging as X  # noqa: E402


def test_parse_definition_note_no_title():
    dx, raw, ni, title = X._parse_definition("[DX835100] 4-1. 금융상품 위험 관리 | 4-1. Financial risk management")
    assert dx == "DX835100"
    assert raw == "4-1" and ni == 4
    assert title == "금융상품 위험 관리"  # 영문 라벨 절단


def test_parse_definition_merged_and_plain():
    # 병합 주석 "13.14." → 정수 기준 13
    _, raw, ni, _ = X._parse_definition("[DX816000] 13.14. 당기손익-공정가치측정금융부채")
    assert raw == "13.14" and ni == 13
    # 번호 없는 세부공시 라벨 → note_no None
    _, raw2, ni2, t2 = X._parse_definition("[DX835100a] 공정가치 서열체계 수준의 정의")
    assert raw2 is None and ni2 is None and t2 == "공정가치 서열체계 수준의 정의"


def test_fs_kind_rule():
    # DX 끝자리 0=연결, 5=별도. 서브롤 접미(letter)는 base 기준.
    assert X._fs_kind("DX835100") == "연결"
    assert X._fs_kind("DX835105") == "별도"
    assert X._fs_kind("DX805005") == "별도"
    assert X._fs_kind("DX835100a") == "연결"
    assert X._fs_kind("DX835105b") == "별도"
    assert X._base_dx("DX835100a") == "DX835100"


def test_href_to_qname():
    assert X._href_to_qname("x.xsd#ifrs-full_FinancialAssets") == "ifrs-full:FinancialAssets"
    assert X._href_to_qname("e.xsd#entity00382199_SomeConcept") == "entity00382199:SomeConcept"
    assert X._href_to_qname("nofrag") is None


def test_split_tag():
    assert X._split_tag("{http://ns}Local") == ("http://ns", "Local")
    assert X._split_tag("plain") == ("", "plain")


def _roles(*specs):
    """(dx, note_no, note_int, title) 스펙 → role_types dict (build_l1/L3 용)."""
    out = {}
    for dx, note_no, note_int, title in specs:
        base = X._base_dx(dx)
        out[dx] = {"dx": dx, "base": base, "suffix": dx[len(base):],
                   "note_no": note_no, "note_int": note_int, "title": title,
                   "fs_kind": X._fs_kind(dx), "is_note": base[:3] == "DX8"}
    return out


def test_build_l1_declared_vs_used():
    rt = _roles(
        ("DX220000", None, None, "재무상태표"),         # 본문(연결)
        ("DX804000", "6", 6, "영업부문 정보"),           # 주석 연결 사용
        ("DX819000", "9", 9, "파생상품"),                # 주석 연결 미사용
        ("DX835100a", None, None, "세부공시"),           # 번호없는 서브공시(차집합 제외)
    )
    used = {"DX220000", "DX804000"}
    l1 = X.build_l1(rt, used)
    assert l1["notes_declared"] == 3 and l1["notes_used"] == 1
    assert l1["statements_declared"] == 1 and l1["statements_used"] == 1
    # 차집합엔 번호 있는 미사용 주석(DX819000)만 — 서브공시는 제외
    dxs = [r["dx"] for r in l1["declared_not_used"]]
    assert dxs == ["DX819000"]


def test_map_pdf_to_xbrl_strong_and_div_scoped():
    rt = _roles(
        ("DX804000", "6", 6, "영업부문 정보"),     # 연결
        ("DX805005", "5", 5, "현금 및 예치금"),     # 별도
    )
    used = {"DX804000", "DX805005"}
    pdf = [
        {"no": 6, "title": "영업부문 정보", "fs_div": "연결"},
        {"no": 5, "title": "현금 및 예치금", "fs_div": "별도"},
        {"no": 99, "title": "회사의 개요", "fs_div": "연결"},  # 미태깅(서술형)
    ]
    res = X.map_pdf_to_xbrl(pdf, rt, used)
    assert res["overall"] == {"matched": 2, "total": 3, "rate": round(2/3, 3)}
    matched = {(m["pdf_no"], m["xbrl_dx"], m["match"]) for m in res["matched"]}
    assert (6, "DX804000", "강") in matched
    assert (5, "DX805005", "강") in matched      # 별도 note가 별도 롤과만 매칭
    assert [p["pdf_no"] for p in res["pdf_only"]] == [99]


def test_map_pdf_sibling_role_not_false_gap():
    # PDF note 4 가 4-1 롤에 매칭되면 형제 4-2(같은 fs_div·note_int=4)는 xbrl_only 에서 제외.
    rt = _roles(
        ("DX835100", "4-1", 4, "금융상품 위험 관리"),
        ("DX835200", "4-2", 4, "금융상품 위험 관리 - 공정가치"),
        ("DX837000", "31", 31, "특수관계자 거래"),  # PDF 에 없음 → 진짜 xbrl_only
    )
    used = {"DX835100", "DX835200", "DX837000"}
    pdf = [{"no": 4, "title": "금융상품 위험 관리", "fs_div": "연결"}]
    res = X.map_pdf_to_xbrl(pdf, rt, used)
    only = {m["xbrl_dx"] for m in res["xbrl_only"]}
    assert "DX835200" not in only      # 형제 세부롤 → 거짓 갭 아님
    assert "DX837000" in only          # 진짜 미대응


# ----- L2 차원 인지 라우팅(dimension-aware) -----
def test_route_fact_concept_only():
    hc = {
        "DX819000": {"primary_items": {"dart:NotionalAmount"}, "axes": {"ifrs-full:FsAxis"}},
        "DX804000": {"primary_items": {"ifrs-full:SegmentRevenue"}, "axes": {"ifrs-full:FsAxis"}},
    }
    # 개념이 한 롤에만 → 그 롤
    assert X.route_fact("dart:NotionalAmount", frozenset({"ifrs-full:FsAxis"}), hc) == "DX819000"
    # 어느 롤에도 없는 개념(본문) → None
    assert X.route_fact("ifrs-full:Assets", frozenset(), hc) is None


def test_route_fact_tightest_axis_superset():
    # 공유 개념(FinancialAssets)이 공정가치(축2)·신용위험(축4) 두 롤의 primary item.
    hc = {
        "DX835200": {"primary_items": {"ifrs-full:FinancialAssets"},
                     "axes": {"ifrs-full:FsAxis", "ifrs-full:FairValueLevelAxis"}},
        "DX835100": {"primary_items": {"ifrs-full:FinancialAssets"},
                     "axes": {"ifrs-full:FsAxis", "ifrs-full:CustomerAxis",
                              "ifrs-full:CreditGradeAxis", "ifrs-full:EclAxis"}},
    }
    # 신용등급 축이 있는 fact → 신용위험 롤(835100)만 축 ⊇
    credit = frozenset({"ifrs-full:FsAxis", "ifrs-full:CustomerAxis", "ifrs-full:CreditGradeAxis"})
    assert X.route_fact("ifrs-full:FinancialAssets", credit, hc) == "DX835100"
    # 공정가치 수준 축 fact → 공정가치 롤(835200)
    fv = frozenset({"ifrs-full:FsAxis", "ifrs-full:FairValueLevelAxis"})
    assert X.route_fact("ifrs-full:FinancialAssets", fv, hc) == "DX835200"
    # 축이 연결/별도뿐(총계) → 둘 다 적합, 더 타이트(축 적은) 835200 선택(결정론)
    total = frozenset({"ifrs-full:FsAxis"})
    assert X.route_fact("ifrs-full:FinancialAssets", total, hc) == "DX835200"


def test_build_l2_dim_single_attribution():
    # 단일 귀속이면 롤별 fact 합 == attributed, 중복 없음.
    rt = _roles(
        ("DX819000", "9", 9, "파생상품"),
        ("DX835100", "4-1", 4, "금융상품 위험 관리"),
    )
    hc = {
        "DX819000": {"primary_items": {"dart:Notional"}, "axes": {"ax:Fs"}},
        "DX835100": {"primary_items": {"ifrs:FA"}, "axes": {"ax:Fs", "ax:Grade"}},
    }
    buckets = {
        ("dart:Notional", frozenset({"ax:Fs"})): {"numeric": 10, "textblock": 0, "nil": 0, "total": 10},
        ("ifrs:FA", frozenset({"ax:Fs", "ax:Grade"})): {"numeric": 7, "textblock": 0, "nil": 0, "total": 7},
        ("zzz:Unmapped", frozenset()): {"numeric": 3, "textblock": 0, "nil": 0, "total": 3},
    }
    totals = {"facts": 20, "numeric": 20, "textblock": 0, "nil": 0, "contexts": 5}
    l2 = X.build_l2_dim(rt, hc, buckets, totals, used={"DX819000", "DX835100"})
    assert l2["totals"]["attributed"] == 17 and l2["totals"]["unattributed"] == 3
    assert l2["totals"]["max_axes"] == 2
    by = {r["dx"]: r for r in l2["per_note_role"]}
    assert by["DX819000"]["numeric_facts"] == 10 and by["DX819000"]["axes_count"] == 1
    assert by["DX835100"]["numeric_facts"] == 7 and by["DX835100"]["axes_count"] == 2
