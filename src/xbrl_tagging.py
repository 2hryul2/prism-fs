"""주석 XBRL 상세태깅 진단 엔진 (read-only, 결정론, 오프라인).

DART 가 보고서별로 주석을 XBRL 로 "어디까지" 상세 태깅했는지 3단계로 정량화한다.
원천 데이터는 이미 storage 에 보존된 XBRL 일습(instance·xsd·linkbase)뿐 — 네트워크·LLM·AI 무경유.

  L1 (범위)   : .xsd roleType 가 선언한 주석 롤(DX8xxxxx) vs _pre.xml 가 실제 사용한 롤
                → 선언−사용 차집합 = 이 보고서가 태깅하지 않은 주석.
  L2 (충실도) : _pre.xml 의 concept→role 매핑으로 instance fact 를 롤에 귀속 →
                롤별 숫자/텍스트블록 fact 수·세부공시 서브롤 커버.
  L3 (PDF대조): index.json PDF 주석(no/title/fs_div) ↔ XBRL 주석 롤 결정론 매핑 →
                'PDF엔 있으나 XBRL 미태깅' / 'XBRL엔 있으나 PDF 인덱스 누락' 갭.

설계 근거(실측):
  - roleType definition = `[DXNNNNNN] N. 제목 | English`. 주석=DX8 계열, 본문=DX2/3/5/6.
  - **fs_div 규칙: DX 6자리 끝자리 0=연결, 5=별도** (예: DX835100 연결 ↔ DX835105 별도).
  - fact 는 role 을 직접 안 가짐 → presentation linkbase 의 loc(concept) 로만 롤 귀속.
  - 1차 범위는 dimension-naive(차원 무시, concept 단위 fact 집계).

표준 라이브러리만 사용(xml.etree.ElementTree). exe 번들 영향 없음(순수 파싱).
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

try:
    # 경로 헬퍼 재사용(dev·frozen 일관). import 실패 시(테스트 단독) None.
    from paths import LIBRARY_ROOT
except Exception:  # pragma: no cover - 단위테스트는 명시 경로 사용
    LIBRARY_ROOT = None

# XBRL 링크베이스/xlink 네임스페이스(collect_dart 와 동일 값, 의존 최소화 위해 로컬 정의).
_NS_LINK = "http://www.xbrl.org/2003/linkbase"
_NS_XLINK = "http://www.w3.org/1999/xlink"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

_ROLETYPE_TAG = f"{{{_NS_LINK}}}roleType"
_DEFINITION_TAG = f"{{{_NS_LINK}}}definition"
_PRESLINK_TAG = f"{{{_NS_LINK}}}presentationLink"
_LOC_TAG = f"{{{_NS_LINK}}}loc"
_XLINK_ROLE = f"{{{_NS_XLINK}}}role"
_XLINK_HREF = f"{{{_NS_XLINK}}}href"
_XSI_NIL = f"{{{_NS_XSI}}}nil"

# 역할 코드 추출 — roleURI/xlink:role 안의 DXNNNNNN[접미]. 예: ...role-DX835100a
_DX_RE = re.compile(r"(DX\d+[a-z]?)")
# definition 라벨 파싱 — "[DX804000] 6. 영업부문 정보 | 6. Operating segments"
_DEF_RE = re.compile(r"^\s*\[(DX\d+[a-z]?)\]\s*(.*)$")
# 제목 앞 주석번호 — "6. ...", "4-1. ...", "13.14. ...", "20-1. ..."
_NOTE_NO_RE = re.compile(r"^([0-9][0-9.\-]*)\.\s*(.*)$")
# 공백 정규화(verbatim/제목 대조용) — curate_index_claude._WS 와 동일 취지.
_WS = re.compile(r"\s+")
# 제목 정규화 시 제거할 잡음(공백·중점·괄호 보조표기)
_TITLE_NOISE = re.compile(r"[\s·ㆍ\(\)（）]+")


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
def xbrl_dir(company: str, period: str) -> Path:
    """셀의 xbrl 디렉터리 경로(LIBRARY_ROOT/<회사>/<기간>/xbrl)."""
    if LIBRARY_ROOT is None:
        raise RuntimeError("[xbrl_dir] LIBRARY_ROOT 미설정 — paths import 실패")
    return Path(LIBRARY_ROOT) / company / period / "xbrl"


def find_xbrl_files(xdir: Path) -> Dict[str, Optional[Path]]:
    """xbrl 디렉터리에서 {xsd, pre, instance} 경로 해석. 없으면 값 None.

    instance 는 확장자 .xbrl(.xsd 제외). pre 는 *_pre.xml.
    """
    xdir = Path(xdir)
    out: Dict[str, Optional[Path]] = {"xsd": None, "pre": None, "instance": None}
    if not xdir.exists():
        return out
    for f in sorted(xdir.iterdir()):
        n = f.name.lower()
        if n.endswith(".xsd"):
            out["xsd"] = out["xsd"] or f
        elif n.endswith("_pre.xml"):
            out["pre"] = out["pre"] or f
        elif n.endswith(".xbrl"):
            out["instance"] = out["instance"] or f
    return out


# ---------------------------------------------------------------------------
# 파싱 헬퍼
# ---------------------------------------------------------------------------
def _dx_of(uri: Optional[str]) -> Optional[str]:
    """roleURI/xlink:role 문자열에서 DX 코드 추출. 없으면 None."""
    if not uri:
        return None
    m = _DX_RE.search(uri)
    return m.group(1) if m else None


def _fs_kind(dx: str) -> str:
    """DX 코드 끝자리로 연결/별도 판정(0=연결, 5=별도). 그 외는 연결로 간주."""
    base = dx[:-1] if dx[-1:].isalpha() else dx  # 서브롤 접미 letter 제거
    return "별도" if base.endswith("5") else "연결"


def _base_dx(dx: str) -> str:
    """서브롤(접미 a~z) → base 롤 코드. 예: DX835100a → DX835100."""
    return dx[:-1] if dx[-1:].isalpha() else dx


def _parse_definition(text: str) -> Tuple[Optional[str], Optional[str], Optional[int], str]:
    """definition 라벨 → (dx, note_no_raw, note_int, title_ko).

    "[DX835100] 4-1. 금융상품 위험 관리 | 4-1. Financial..." →
        (DX835100, "4-1", 4, "금융상품 위험 관리")
    번호 없는 서브공시 라벨은 note_no_raw=None.
    """
    m = _DEF_RE.match(text or "")
    if not m:
        return None, None, None, _WS.sub(" ", (text or "")).strip()
    dx = m.group(1)
    rest = m.group(2)
    ko = rest.split("|")[0].strip()  # 영문 라벨 절단(파이프 뒤)
    nm = _NOTE_NO_RE.match(ko)
    if nm:
        note_no_raw = nm.group(1)
        title = _WS.sub(" ", nm.group(2)).strip()
        im = re.match(r"\d+", note_no_raw)
        note_int = int(im.group(0)) if im else None
        return dx, note_no_raw, note_int, title
    return dx, None, None, _WS.sub(" ", ko).strip()


def _href_to_qname(href: Optional[str]) -> Optional[str]:
    """loc xlink:href 의 '#concept' 조각을 prefix:Local QName 으로 정규화.

    '...full_ifrs-cor_2021-03-24.xsd#ifrs-full_FinancialAssets' → 'ifrs-full:FinancialAssets'
    조각의 첫 '_' 를 prefix 구분자로 사용(IFRS/DART/entity concept 모두 prefix_Local 형식).
    """
    if not href or "#" not in href:
        return None
    frag = href.rsplit("#", 1)[-1]
    if "_" not in frag:
        return None
    prefix, local = frag.split("_", 1)
    return f"{prefix}:{local}"


def _split_tag(tag: str) -> Tuple[str, str]:
    """'{uri}Local' → (uri, Local). 네임스페이스 없으면 ('', tag)."""
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        return uri, local
    return "", tag


# ---------------------------------------------------------------------------
# L1 — 선언(roleType) vs 사용(presentation)
# ---------------------------------------------------------------------------
def parse_role_types(xsd_path: Path) -> Dict[str, Dict[str, Any]]:
    """xsd 의 모든 <link:roleType> → {dx: meta}. 스트리밍 파싱.

    meta: {dx, base, suffix, note_no, note_int, title, fs_kind, is_note}.
    is_note: DX8 계열(주석). 본문(DX2/3/5/6)·기타는 False.
    """
    import xml.etree.ElementTree as ET
    out: Dict[str, Dict[str, Any]] = {}
    for _ev, elem in ET.iterparse(str(xsd_path), events=("end",)):
        if elem.tag != _ROLETYPE_TAG:
            continue
        role_uri = elem.get("roleURI")
        defn = elem.find(_DEFINITION_TAG)
        deftext = defn.text if defn is not None else ""
        dx = _dx_of(role_uri) or _dx_of(deftext)
        if dx:
            _, note_no_raw, note_int, title = _parse_definition(deftext or "")
            base = _base_dx(dx)
            out[dx] = {
                "dx": dx,
                "base": base,
                "suffix": dx[len(base):],
                "note_no": note_no_raw,
                "note_int": note_int,
                "title": title,
                "fs_kind": _fs_kind(dx),
                "is_note": base[:3] == "DX8",
            }
        elem.clear()
    return out


def parse_presentation_roles(pre_path: Path) -> Set[str]:
    """_pre.xml 의 presentationLink 가 실제 사용하는 DX 롤 집합."""
    import xml.etree.ElementTree as ET
    used: Set[str] = set()
    for _ev, elem in ET.iterparse(str(pre_path), events=("end",)):
        if elem.tag == _PRESLINK_TAG:
            dx = _dx_of(elem.get(_XLINK_ROLE))
            if dx:
                used.add(dx)
            elem.clear()
    return used


def build_l1(role_types: Dict[str, Dict[str, Any]], used: Set[str]) -> Dict[str, Any]:
    """선언 롤 + 사용 집합 → L1 요약(주석 declared/used/차집합, 본문 별도 집계)."""
    notes = [m for m in role_types.values() if m["is_note"]]
    stmts = [m for m in role_types.values() if not m["is_note"]]
    notes_used = [m for m in notes if m["dx"] in used]
    declared_not_used = [
        {"dx": m["dx"], "note_no": m["note_no"], "title": m["title"], "fs_kind": m["fs_kind"]}
        for m in notes if m["dx"] not in used and m["note_no"]  # 번호 있는 주석 롤만(서브공시 노이즈 제외)
    ]
    declared_not_used.sort(key=lambda r: r["dx"])
    return {
        "notes_declared": len(notes),
        "notes_used": len(notes_used),
        "used_ratio": round(len(notes_used) / len(notes), 3) if notes else 0.0,
        "statements_declared": len(stmts),
        "statements_used": sum(1 for m in stmts if m["dx"] in used),
        "declared_not_used": declared_not_used,
    }


# ---------------------------------------------------------------------------
# L2 — concept→role 역인덱스 + instance fact 집계
# ---------------------------------------------------------------------------
def build_concept_to_roles(pre_path: Path) -> Dict[str, Set[str]]:
    """_pre.xml 순회 → {concept_qname: {dx,...}}. 한 concept 가 여러 롤에 등장 가능."""
    import xml.etree.ElementTree as ET
    c2r: Dict[str, Set[str]] = {}
    cur_dx: Optional[str] = None
    for ev, elem in ET.iterparse(str(pre_path), events=("start", "end")):
        if ev == "start":
            if elem.tag == _PRESLINK_TAG:
                cur_dx = _dx_of(elem.get(_XLINK_ROLE))
            continue
        # end
        if elem.tag == _LOC_TAG and cur_dx:
            qn = _href_to_qname(elem.get(_XLINK_HREF))
            if qn:
                c2r.setdefault(qn, set()).add(cur_dx)
        elif elem.tag == _PRESLINK_TAG:
            cur_dx = None
            elem.clear()
    return c2r


def count_instance_facts(instance_path: Path) -> Tuple[Dict[str, Dict[str, int]], Dict[str, int]]:
    """instance(.xbrl) → (per_concept, totals). 대용량 스트리밍(iterparse+clear).

    per_concept: {qname: {"numeric","textblock","nil","total"}}.
    totals:      {"facts","numeric","textblock","nil","contexts"}.
    fact 판정 = contextRef 속성 보유. QName prefix 는 start-ns 로 선수집한 uri→prefix 로 복원.
    """
    import xml.etree.ElementTree as ET
    uri2prefix: Dict[str, str] = {}
    per: Dict[str, Dict[str, int]] = {}
    totals = {"facts": 0, "numeric": 0, "textblock": 0, "nil": 0, "contexts": 0}
    for ev, elem in ET.iterparse(str(instance_path), events=("start-ns", "end")):
        if ev == "start-ns":
            prefix, uri = elem  # (prefix, uri) 튜플
            uri2prefix[uri] = prefix
            continue
        # end
        uri, local = _split_tag(elem.tag)
        if local == "context":
            totals["contexts"] += 1
        if elem.get("contextRef") is not None:  # fact
            prefix = uri2prefix.get(uri, "")
            qn = f"{prefix}:{local}" if prefix else local
            is_tb = local.endswith("TextBlock")
            is_nil = elem.get(_XSI_NIL) == "true"
            rec = per.setdefault(qn, {"numeric": 0, "textblock": 0, "nil": 0, "total": 0})
            rec["total"] += 1
            totals["facts"] += 1
            if is_nil:
                rec["nil"] += 1
                totals["nil"] += 1
            elif is_tb:
                rec["textblock"] += 1
                totals["textblock"] += 1
            else:
                rec["numeric"] += 1
                totals["numeric"] += 1
        elem.clear()
    return per, totals


def build_l2(role_types: Dict[str, Dict[str, Any]],
             concept_to_roles: Dict[str, Set[str]],
             per_concept: Dict[str, Dict[str, int]],
             used: Set[str]) -> Dict[str, Any]:
    """concept별 fact 를 롤(base 주석)로 롤업 → 주석 롤별 충실도.

    base 롤별 집계(서브롤 a~z 는 base 로 합산). 사용된 주석 롤만 보고.
    """
    # base 롤별 누적기. note_int/title/fs_kind 는 base meta 에서.
    agg: Dict[str, Dict[str, Any]] = {}

    def _ensure(base_dx: str) -> Optional[Dict[str, Any]]:
        meta = role_types.get(base_dx)
        if meta is None or not meta["is_note"]:
            return None
        return agg.setdefault(base_dx, {
            "dx": base_dx, "title": meta["title"], "note_no": meta["note_no"],
            "note_int": meta["note_int"], "fs_kind": meta["fs_kind"],
            "concepts": 0, "concepts_with_facts": 0,
            "numeric_facts": 0, "textblock_facts": 0,
            "subroles_total": 0, "subroles_covered": 0,
        })

    # 서브롤 총수/커버: role_types 중 base 동일한 것들
    sub_by_base: Dict[str, List[str]] = {}
    for dx, meta in role_types.items():
        if meta["is_note"]:
            sub_by_base.setdefault(meta["base"], []).append(dx)

    # concept → 그 concept 가 속한 롤들 → base 로 귀속해 fact 합산
    for qn, roles in concept_to_roles.items():
        facts = per_concept.get(qn)
        for dx in roles:
            base = _base_dx(dx)
            rec = _ensure(base)
            if rec is None:
                continue
            rec["concepts"] += 1
            if facts and facts["total"] > 0:
                rec["concepts_with_facts"] += 1
                rec["numeric_facts"] += facts["numeric"]
                rec["textblock_facts"] += facts["textblock"]

    # 서브롤 커버(해당 base 의 서브롤 중 사용된 것 비율)
    for base, rec in agg.items():
        subs = sub_by_base.get(base, [base])
        rec["subroles_total"] = len(subs)
        rec["subroles_covered"] = sum(1 for s in subs if s in used)

    per_note_role = sorted(agg.values(), key=lambda r: r["dx"])
    return {"per_note_role": per_note_role}


# ---------------------------------------------------------------------------
# L3 — PDF index.json ↔ XBRL 주석 롤 매핑
# ---------------------------------------------------------------------------
def _norm_title(t: str) -> str:
    """제목 정규화 — 공백·중점·괄호 제거(부분문자열 대조용)."""
    return _TITLE_NOISE.sub("", t or "")


def map_pdf_to_xbrl(pdf_notes: List[Dict[str, Any]],
                    role_types: Dict[str, Dict[str, Any]],
                    used: Set[str]) -> Dict[str, Any]:
    """PDF 주석(index.json notes) ↔ 사용된 XBRL 주석 base 롤 매핑(fs_div 안에서, 결정론).

    매칭 우선순위(fs_div 동일 전제):
      1) note_int 일치 AND 제목 유사(부분문자열 어느 한쪽)  → 강매칭
      2) 제목 강유사(정규화 동등 또는 한쪽이 다른 쪽 포함)   → 제목매칭
      3) note_int 일치만                                    → 약매칭
    한 PDF 주석이 ≥1 XBRL 롤과 매칭되면 matched. AI/임베딩 미사용.
    """
    # 사용된 주석 base 롤만(서브롤 제외, note_no 보유)
    xbrl_roles = []
    for dx, m in role_types.items():
        if m["is_note"] and dx == m["base"] and dx in used and m["note_no"]:
            xbrl_roles.append(m)

    matched, pdf_only = [], []
    matched_dx: Set[str] = set()
    matched_note_keys: Set[Tuple[Optional[str], Optional[int]]] = set()  # (fs_div, note_int)

    for n in pdf_notes:
        fs = n.get("fs_div")
        pno = n.get("no")
        ptitle = n.get("title", "")
        pnorm = _norm_title(ptitle)
        best = None
        best_rank = 99
        for m in xbrl_roles:
            if m["fs_kind"] != fs:
                continue
            xnorm = _norm_title(m["title"])
            no_eq = (m["note_int"] is not None and pno is not None and m["note_int"] == pno)
            title_sim = bool(pnorm) and bool(xnorm) and (pnorm in xnorm or xnorm in pnorm or pnorm == xnorm)
            if no_eq and title_sim:
                rank = 1
            elif title_sim:
                rank = 2
            elif no_eq:
                rank = 3
            else:
                continue
            if rank < best_rank:
                best, best_rank = m, rank
        if best is not None:
            matched.append({
                "fs_div": fs, "pdf_no": pno, "pdf_title": ptitle,
                "xbrl_dx": best["dx"], "xbrl_note_no": best["note_no"],
                "xbrl_title": best["title"], "match": ["", "강", "제목", "약"][best_rank],
            })
            matched_dx.add(best["dx"])
            matched_note_keys.add((fs, best["note_int"]))
        else:
            pdf_only.append({"fs_div": fs, "pdf_no": pno, "pdf_title": ptitle})

    # xbrl_only = 사용된 주석 롤 중 매칭 안 됨. 단, 같은 (fs_div, note_int)가 이미 매칭됐으면
    # 형제 세부롤(예: 4-2/4-3)이므로 제외(PDF가 부모 주석으로 이미 커버 → 거짓 갭 방지).
    xbrl_only = [
        {"fs_div": m["fs_kind"], "xbrl_dx": m["dx"],
         "xbrl_note_no": m["note_no"], "xbrl_title": m["title"]}
        for m in sorted(xbrl_roles, key=lambda r: r["dx"])
        if m["dx"] not in matched_dx
        and (m["fs_kind"], m["note_int"]) not in matched_note_keys
    ]

    def _rate(div: Optional[str]) -> Dict[str, Any]:
        notes = [n for n in pdf_notes if div is None or n.get("fs_div") == div]
        mm = [x for x in matched if div is None or x["fs_div"] == div]
        total = len(notes)
        return {"matched": len(mm), "total": total,
                "rate": round(len(mm) / total, 3) if total else 0.0}

    return {
        "overall": _rate(None),
        "by_div": {"연결": _rate("연결"), "별도": _rate("별도")},
        "matched": matched,
        "pdf_only": pdf_only,
        "xbrl_only": xbrl_only,
    }


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------
def diagnose_files(xsd_path: Path, pre_path: Path, instance_path: Path,
                   pdf_notes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """명시 경로 + PDF notes 로 L1/L2/L3 합본 산출(테스트·재사용용 순수 진입점)."""
    role_types = parse_role_types(xsd_path)
    used = parse_presentation_roles(pre_path)
    l1 = build_l1(role_types, used)

    concept_to_roles = build_concept_to_roles(pre_path)
    per_concept, totals = count_instance_facts(instance_path)
    l2 = build_l2(role_types, concept_to_roles, per_concept, used)
    l2["totals"] = totals

    l3 = map_pdf_to_xbrl(pdf_notes, role_types, used)
    return {"status": "ok", "l1": l1, "l2": l2, "l3": l3}


def _load_pdf_notes(company: str, period: str) -> List[Dict[str, Any]]:
    """index.json 의 notes[] 로드(없으면 빈 리스트)."""
    if LIBRARY_ROOT is None:
        return []
    idx = Path(LIBRARY_ROOT) / company / period / "index.json"
    if not idx.exists():
        return []
    with open(idx, "r", encoding="utf-8") as f:
        return json.load(f).get("notes", [])


def diagnose_one(company: str, period: str) -> Dict[str, Any]:
    """셀(회사·기간) 진단. XBRL 파일 누락 시 status='missing_xbrl'."""
    files = find_xbrl_files(xbrl_dir(company, period))
    head = {"company": company, "period": period,
            "files": {k: (v.name if v else None) for k, v in files.items()}}
    if not (files["xsd"] and files["pre"] and files["instance"]):
        return {**head, "status": "missing_xbrl"}
    pdf_notes = _load_pdf_notes(company, period)
    body = diagnose_files(files["xsd"], files["pre"], files["instance"], pdf_notes)
    return {**head, **body, "pdf_notes_count": len(pdf_notes)}
