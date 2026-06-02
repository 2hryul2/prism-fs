"""Claude 큐레이션 인덱싱 CLI (개발 PC 전용).

에이전트가 작성한 notes 초안(notes.json)을 받아 로컬에서만:
  - validate: PDF 본문 대조 + 결정론 규칙 검증(verbatim·숫자금지·페이지경계·fs_div·단조)
  - diff: 현재 index.json 의 notes 와 초안 비교(추가/삭제/변경)
  - apply: 검증 통과 시 로컬 임베딩으로 index.json(schema=2) 재생성

네트워크·외부 API(DART/LLM) 호출 절대 없음. 임베딩은 app 의 로컬 모델만 사용.
exe 번들 제외 — prism_fs.spec datas/hiddenimports 에 추가하지 말 것.

사용 예:
  python scripts\\curate_index_claude.py validate notes.json
  python scripts\\curate_index_claude.py diff notes.json
  python scripts\\curate_index_claude.py apply notes.json --yes
"""
import re
import sys
import json
import shutil
import asyncio
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# 공백 정규화 — verbatim 대조 시 양쪽에 동일 적용(레이아웃 공백차 흡수).
_WS = re.compile(r"\s+")
# 숫자/금액성 제목 패턴 — 제목은 본문 문구여야지 수치여선 안 됨.
_NUMERIC_TITLE = re.compile(r"^[\d,.\s%()\-]+$")
_VALID_FS_DIV = ("연결", "별도")


def _norm(text: str) -> str:
    """공백 전부 제거(verbatim 포함 여부 판정용)."""
    return _WS.sub("", text or "")


def curate_validate(notes: list, total_pages: int, page_text_provider) -> tuple:
    """(violations, warnings) 튜플 반환. app import 불요한 순수 함수.

    하드 위반(violations)은 apply 를 차단하고, 경고(warnings)는 차단하지 않는다.
    brief 의 "경고(advisory)" 정의와 일치시키기 위해 두 채널을 분리.

    Args:
        notes: [{no,title,page_start,page_end,fs_div,topic?}, ...]
        total_pages: PDF 총 페이지 수(1-based 경계 검증용)
        page_text_provider: (p_start, p_end) -> str. 해당 페이지 범위 본문 텍스트.
    Returns:
        (violations, warnings): 둘 다 문자열 리스트.
        - violations(차단): verbatim 위반, 숫자성 제목, 페이지 경계, 정수 아님, fs_div 오류.
        - warnings(미차단): fs_div 내 page_start 역행, (no, fs_div) 중복.
    """
    violations = []
    warnings = []
    last_page_by_div = {}      # fs_div 별 직전 page_start (단조 비감소 검사용)
    seen_keys = set()          # (no, fs_div) 중복 검사용

    for n in notes:
        no = n.get("no")
        title = n.get("title", "")
        p_start = n.get("page_start")
        p_end = n.get("page_end")
        fs_div = n.get("fs_div")
        tag = f"[no={no} '{title}']"

        # ② 숫자/금액성 제목 금지 (먼저 검사 → 수치 제목은 verbatim 의미 없음)
        if title and _NUMERIC_TITLE.match(title):
            violations.append(f"{tag} 숫자성 제목 금지")

        # ③ 페이지 경계: 1 ≤ page_start ≤ page_end ≤ total_pages
        if not (isinstance(p_start, int) and isinstance(p_end, int)):
            violations.append(f"{tag} page_start/page_end 정수 아님")
        elif not (1 <= p_start <= p_end <= total_pages):
            violations.append(
                f"{tag} 페이지 경계 위반 (start={p_start}, end={p_end}, total={total_pages})"
            )
        else:
            # ① verbatim: 제목(공백제거)이 해당 페이지 본문(공백제거)에 포함되어야 함
            #    (경계 통과 시에만 검사 — 잘못된 페이지 본문 대조는 무의미)
            body = _norm(page_text_provider(p_start, p_end))
            if _norm(title) not in body:
                violations.append(f"{tag} 본문에 제목 문구 없음(verbatim 위반)")

            # ⑤ 같은 fs_div 내 page_start 단조 비감소 — 경고(미차단)
            prev = last_page_by_div.get(fs_div)
            if prev is not None and p_start < prev:
                warnings.append(
                    f"{tag} 경고: {fs_div} 내 page_start 역행 ({prev} → {p_start})"
                )
            last_page_by_div[fs_div] = p_start

        # ④ fs_div 허용 목록
        if fs_div not in _VALID_FS_DIV:
            violations.append(f"{tag} fs_div 오류: {fs_div!r} (연결|별도)")

        # ⑥ (no, fs_div) 중복 — 경고(미차단)
        key = (no, fs_div)
        if key in seen_keys:
            warnings.append(f"{tag} 경고: (no={no}, fs_div={fs_div}) 중복")
        seen_keys.add(key)

    return violations, warnings


def _load_notes_doc(notes_json: Path) -> dict:
    """notes.json 로드 + 필수 헤더 검증."""
    if not notes_json.exists():
        raise FileNotFoundError(f"[_load_notes_doc] notes.json 없음: {notes_json}")
    with open(notes_json, "r", encoding="utf-8") as f:
        doc = json.load(f)
    for k in ("company", "period", "notes"):
        if k not in doc:
            raise ValueError(f"[_load_notes_doc] 필수 키 누락: {k} (파일: {notes_json})")
    return doc


def _make_page_text_provider(app, company: str, period: str):
    """app.pdf_path 의 PDF 를 열어 (p_start,p_end)->본문텍스트 provider 구성.

    fitz 는 0-based, notes 페이지는 1-based 이므로 [p_start-1, p_end) 슬라이스.
    """
    import fitz
    src_pdf = app.pdf_path(company, period, "report")
    if not src_pdf.exists():
        raise FileNotFoundError(f"[_make_page_text_provider] PDF 없음: {src_pdf}")
    doc = fitz.open(src_pdf)

    def provider(p_start: int, p_end: int) -> str:
        lo = max((p_start or 1) - 1, 0)
        hi = min(p_end or lo + 1, doc.page_count)
        return "\n".join(doc[p].get_text() for p in range(lo, hi))

    return provider, doc.page_count, doc


def _import_app():
    """src 를 sys.path 에 추가 후 app import(임베딩/경로 헬퍼 재사용)."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import app
    return app


def cmd_validate(args) -> int:
    doc = _load_notes_doc(Path(args.notes_json))
    app = _import_app()
    provider, total_pages, fitz_doc = _make_page_text_provider(
        app, doc["company"], doc["period"]
    )
    try:
        violations, warnings = curate_validate(doc["notes"], total_pages, provider)
    finally:
        fitz_doc.close()

    for w in warnings:
        print("  ! " + w)
    if not violations:
        print(
            f"검증 통과: notes {len(doc['notes'])}건 "
            f"(total_pages={total_pages}, 경고 {len(warnings)}건)"
        )
        return 0
    print(f"검증 실패: 위반 {len(violations)}건")
    for m in violations:
        print("  - " + m)
    # 위반(verbatim/숫자/경계/fs_div)이 하나라도 있으면 비0 종료. 경고만이면 통과.
    return 1


def _index_notes(app, company: str, period: str) -> list:
    """현재 index.json 의 notes(없으면 빈 리스트)."""
    idx = app.index_path(company, period, "report")
    if not idx.exists():
        return []
    with open(idx, "r", encoding="utf-8") as f:
        return json.load(f).get("notes", [])


def _diff_key(n: dict) -> tuple:
    """diff 매칭 키 — (fs_div, title). 연결/별도 동일 제목 충돌 방지."""
    return (n.get("fs_div"), n.get("title"))


def _fmt_key(key: tuple) -> str:
    """(fs_div, title) 키를 사람이 읽을 출력 문자열로."""
    fs_div, title = key
    return f"({fs_div}) {title}"


def _diff_notes(current: list, draft: list) -> dict:
    """(fs_div, title) 기준 매칭으로 추가/삭제/변경 산출."""
    cur_by_key = {_diff_key(n): n for n in current}
    draft_by_key = {_diff_key(n): n for n in draft}

    added = [k for k in draft_by_key if k not in cur_by_key]
    removed = [k for k in cur_by_key if k not in draft_by_key]
    changed = []
    for k in draft_by_key:
        if k in cur_by_key:
            a, b = cur_by_key[k], draft_by_key[k]
            for field in ("page_start", "page_end"):
                if a.get(field) != b.get(field):
                    changed.append((k, field, a.get(field), b.get(field)))
    return {"added": added, "removed": removed, "changed": changed}


def _print_diff(current: list, draft: list):
    d_conn = sum(1 for n in draft if n.get("fs_div") == "연결")
    d_sep = sum(1 for n in draft if n.get("fs_div") == "별도")
    c_conn = sum(1 for n in current if n.get("fs_div") == "연결")
    c_sep = sum(1 for n in current if n.get("fs_div") == "별도")
    print(f"현재 index : 연결 {c_conn} / 별도 {c_sep} (총 {len(current)})")
    print(f"초안 notes : 연결 {d_conn} / 별도 {d_sep} (총 {len(draft)})")

    d = _diff_notes(current, draft)
    print(f"추가 {len(d['added'])} / 삭제 {len(d['removed'])} / 변경 {len(d['changed'])}")
    for k in d["added"]:
        print(f"  [+] {_fmt_key(k)}")
    for k in d["removed"]:
        print(f"  [-] {_fmt_key(k)}")
    for k, field, old, new in d["changed"]:
        print(f"  [~] {_fmt_key(k)} :: {field} {old} -> {new}")


def cmd_diff(args) -> int:
    doc = _load_notes_doc(Path(args.notes_json))
    app = _import_app()
    current = _index_notes(app, doc["company"], doc["period"])
    _print_diff(current, doc["notes"])
    return 0


# 후속(주석 이후) 섹션 헤더 키워드 — 마지막 주석의 page_end 상한 탐지용.
_POST_NOTES_KW = ("배당에 관한 사항", "회사의 배당정책", "증권의 발행", "재무에 관한 사항",
                  "외부감사에 관한", "이사회에 관한 사항")
_SUBHEAD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})\.\s*([가-힣A-Za-z].*)")
# 병합 헤더 컷용 — 제목 내부에 또 다른 'NN-N.' 가 나오면 그 앞까지만.
_EMBED_SUBHEAD = re.compile(r"\s*\d{1,2}-\d{1,2}\.")
_POLICY_TITLES = ("재무제표 작성기준", "중요한 회계정책")
# 재무제표 본문 제목(노트 아님) — '~에 대한 주석'이 아니면 제외.
_STATEMENT_KW = ("재무상태표", "포괄손익계산서", "손익계산서", "자본변동표", "현금흐름표",
                 "이익잉여금처분계산서", "결손금처리계산서")


def _is_statement_title(title: str) -> bool:
    """재무제표 본문 제목이면 True(단, '주석'/'대한' 포함 시 노트로 간주해 False)."""
    t = title or ""
    if "주석" in t or "대한" in t:
        return False
    return any(k in t for k in _STATEMENT_KW)


def _auto_draft(app, company: str, period: str) -> dict:
    """기존 index.json notes + 감지된 N-x 서브헤더 + 비접미 정책노트를 모아
    페이지순 next-1 로 page_end 재계산(마지막은 후속 섹션 앞에서 컷)한 초안 dict 생성.

    제목은 모두 PDF 원문 헤더에서 온 verbatim. 네트워크/LLM 없음.
    """
    import fitz
    src_pdf = app.pdf_path(company, period, "report")
    doc = fitz.open(src_pdf)
    total = doc.page_count
    cur_idx = _index_notes(app, company, period)
    # 1) 시작점: 기존 notes (page_start·fs_div 신뢰). 병합 제목은 개별 노트로 분해
    #    (서브노트 page_start 는 원 노트 범위에서 실제 등장 페이지로 재탐색). page_end 는 뒤에서 재계산.
    kept = {}  # (fs_div, page_start, title) -> note
    for n in cur_idx:
        fs = n.get("fs_div")
        lo = n.get("page_start")
        hi = n.get("page_end") or lo
        for sub_no, sub_title in _split_merged_title(n.get("no"), n.get("title")):
            ps = _resolve_subnote_page(doc, sub_title, lo, hi)
            kept.setdefault((fs, ps, sub_title), {
                "no": sub_no, "title": sub_title, "page_start": ps, "fs_div": fs,
            })
    # 연결/별도 영역 경계(별도 첫 page_start)
    sep_starts = [n["page_start"] for n in cur_idx if n.get("fs_div") == "별도"]
    sep_start = min(sep_starts) if sep_starts else total + 1

    def _fsdiv_for(page):
        return "별도" if page >= sep_start else "연결"

    # 주석 영역 경계 — 기존 연결/별도 주석 page_start 범위로 한정(재무제표 본문·후속 섹션 배제)
    conn_lo = min((n["page_start"] for n in cur_idx if n.get("fs_div") == "연결"), default=1)
    # 후속 섹션 시작 페이지(마지막 주석 상한)
    post_page = total + 1
    last_note_start = max((n["page_start"] for n in cur_idx), default=1)
    for c in app._collect_header_candidates(doc):
        if c["page"] >= last_note_start and any(k in c["title"] for k in _POST_NOTES_KW):
            post_page = min(post_page, c["page"])

    def _in_notes_region(page):
        # 연결주석 시작~연결주석끝(별도시작-1), 또는 별도주석 시작~후속섹션-1
        if conn_lo <= page < sep_start:
            return True
        if sep_start <= page < post_page:
            return True
        return False

    # 2) N-x 서브헤더 복구(예: 4-1/4-2/4-3 금융위험관리). 주석 영역 + 비-재무제표 제목만.
    for p in range(total):
        if not _in_notes_region(p + 1):
            continue
        for ln in doc[p].get_text().splitlines()[:5]:
            m = _SUBHEAD_RE.match(ln.strip())
            if not m:
                continue
            # _clean_head(점선·접미 컷) → _clean_title('부모,자식' 서브표 정리) 일관 적용
            title = _clean_title(_clean_head(m.group(3)))
            if len(_norm(title)) < 2 or _is_statement_title(title):
                continue
            fs = _fsdiv_for(p + 1)
            key = (fs, p + 1, title)
            kept.setdefault(key, {"no": int(m.group(1)), "title": title,
                                  "page_start": p + 1, "fs_div": fs})
    # 3) 비접미 정책노트(작성기준/회계정책) 복구 — 주석 영역 내 헤더 후보에서.
    for c in app._collect_header_candidates(doc):
        if c.get("fs_div") or not _in_notes_region(c["page"]):
            continue
        title = _clean_head(c["title"])
        if not any(t in title for t in _POLICY_TITLES):
            continue
        fs = _fsdiv_for(c["page"])
        key = (fs, c["page"], title)
        kept.setdefault(key, {"no": c["no"], "title": title,
                              "page_start": c["page"], "fs_div": fs})
    # 5) 페이지순 정렬 후 page_end = 다음 시작 -1, 마지막은 post_page-1
    notes = sorted(kept.values(), key=lambda n: (n["page_start"], n["no"]))
    for i, n in enumerate(notes):
        nxt = notes[i + 1]["page_start"] if i + 1 < len(notes) else post_page
        n["page_end"] = max(n["page_start"], min(nxt - 1, total))
    doc.close()
    existing_full = None
    idxp = app.index_path(company, period, "report")
    if idxp.exists():
        with open(idxp, "r", encoding="utf-8") as f:
            existing_full = json.load(f)
    return {
        "company": company, "period": period, "doc_type": "report",
        "source_type": (existing_full or {}).get("source_type", ""),
        "detected_unit": (existing_full or {}).get("detected_unit", ""),
        "notes": notes,
    }


def _clean_head(title: str) -> str:
    """헤더 제목 정리 — 점선 leader·(연결)/(별도) 접미·병합 서브헤더 꼬리 제거. 60자 상한."""
    t = re.split(r"\.{3,}|…", title)[0]      # 점선 leader 이후 제거
    t = _EMBED_SUBHEAD.split(t)[0]            # 'NN-N.' 병합 시 앞부분만(헤더 병합 컷)
    t = re.sub(r"\s*\((연결|별도)\)\s*$", "", t)
    return t.strip()[:60]


# 병합 제목 분리용 — 제목 내부에 ', NN. ' (다른 노트번호) 가 박히면 노트 경계로 간주.
# 예: "차입부채, 26. 사채" → [(_, "차입부채"), (26, "사채")]
_MERGED_NOTE = re.compile(r"\s*,\s*(\d{1,2})\.\s+")


def _split_merged_title(no, title: str) -> list:
    """노트번호가 박혀 병합된 제목을 (no, title) 리스트로 분해.

    휴리스틱 추출이 인접 헤더를 ', NN. ' 로 이어붙인 산물을 개별 노트로 복원.
    분해된 각 제목은 원문 헤더의 부분문자열 → verbatim 유지(검증 통과).
    번호 병합이 없으면 [(no, _clean_title(title))] 1건 반환.
    """
    parts = _MERGED_NOTE.split(title or "")
    head = _clean_title(parts[0])
    out = [(no, head)] if len(_norm(head)) >= 2 else []
    # split 결과: [seg0, num1, seg1, num2, seg2, ...] — 홀수 인덱스=번호, 다음=제목
    for i in range(1, len(parts), 2):
        seg = _clean_title(parts[i + 1]) if i + 1 < len(parts) else ""
        # 60자 절단 잔재("...21. 이")는 1글자 꼬리로 남음 → 드롭(가비지 제목 방지)
        if len(_norm(seg)) >= 2:
            out.append((int(parts[i]), seg))
    return out or [(no, _clean_title(title))]


def _clean_title(title: str) -> str:
    """단일 제목 정리 — 후행 쉼표 제거 후, '부모, 자식' 서브표 형식이면 자식(마지막)만.

    PDF N-x 헤더가 "기타부채, 리스부채" 처럼 [부모,자식] 으로 표기됨 → 부모 접두는
    노트번호가 묶어주므로 중복. 가장 구체적인 자식 라벨만 남겨 검색성↑·중복 제거.
    예: "재무정보 요약 사항 기술," → "재무정보 요약 사항 기술"(후행 쉼표만 제거)
        "기타부채, 리스부채" → "리스부채" / "...충당부채, 충당부채" → "충당부채"
    (번호 병합 "A, 5. B" 는 _split_merged_title 가 선분해하므로 여기 쉼표는 서브표뿐.)
    """
    t = (title or "").strip().rstrip(",").strip()
    if "," in t:
        t = t.split(",")[-1].strip()
    return t


def _resolve_subnote_page(doc, title: str, lo: int, hi: int) -> int:
    """분해된 서브노트 제목이 실제로 등장하는 첫 페이지(1-based)를 [lo,hi]에서 탐색.

    찾으면 그 페이지, 못 찾으면 lo 반환(원 노트 시작으로 폴백 → page_end 재계산이 흡수).
    """
    key = _norm(title)
    for p in range(max(lo, 1), min(hi, doc.page_count) + 1):
        if key in _norm(doc[p - 1].get_text()):
            return p
    return lo


def cmd_auto(args) -> int:
    app = _import_app()
    draft = _auto_draft(app, args.company, args.period)
    out = app.index_path(args.company, args.period, "report").parent / "notes_auto.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    print(f"초안 작성: {out}  (notes {len(draft['notes'])}건)")
    # 검증 + diff 출력(리뷰용)
    provider, total_pages, fitz_doc = _make_page_text_provider(app, args.company, args.period)
    try:
        violations, warnings = curate_validate(draft["notes"], total_pages, provider)
    finally:
        fitz_doc.close()
    if violations:
        print(f"  검증 위반 {len(violations)}건:")
        for m in violations[:10]:
            print("   - " + m)
    else:
        print(f"  검증 통과(경고 {len(warnings)}건)")
    _print_diff(_index_notes(app, args.company, args.period), draft["notes"])
    return 0


def cmd_apply(args) -> int:
    doc = _load_notes_doc(Path(args.notes_json))
    app = _import_app()
    company, period = doc["company"], doc["period"]
    source_type = doc.get("source_type", "")
    detected_unit = doc.get("detected_unit", "")

    # 0) 빈 notes 가드 — 빈 index.json 으로 덮어쓰는 오조작 방지
    if not doc["notes"]:
        print("경고: notes 0건 — apply 할 내용이 없습니다. 중단.")
        return 1

    # 1) 검증 필수 — 위반(violations) 시 즉시 중단(재인덱싱 진입 금지)
    #    경고(warnings)는 출력만 하고 진행.
    provider, total_pages, fitz_doc = _make_page_text_provider(app, company, period)
    try:
        violations, warnings = curate_validate(doc["notes"], total_pages, provider)
    finally:
        fitz_doc.close()
    for w in warnings:
        print("  ! " + w)
    if violations:
        print(f"검증 실패(위반 {len(violations)}건) — apply 중단")
        for m in violations:
            print("  - " + m)
        return 1

    # 2) 비대화형 가드 — --yes 없으면 diff 만 보여주고 종료
    current = _index_notes(app, company, period)
    _print_diff(current, doc["notes"])
    if not args.yes:
        print("apply 하려면 --yes 를 붙여 다시 실행하세요.")
        return 0

    # 3) 기존 index.json 백업(있을 때만) → index_heuristic.bak.json
    idx_path = app.index_path(company, period, "report")
    if idx_path.exists():
        bak = idx_path.parent / "index_heuristic.bak.json"
        shutil.copy2(idx_path, bak)
        print(f"백업: {bak}")

    # 4) topic 보존하며 임베딩용 notes 구성(불필요 키 제거 없이 그대로 전달)
    notes = doc["notes"]
    src_pdf = app.pdf_path(company, period, "report")
    res = asyncio.run(app.embed_and_write_index(
        company, period, "report", notes, detected_unit, source_type, src_pdf,
    ))
    print(
        f"완료: notes {res['notes_count']}건 "
        f"(연결 {res['n_conn']} / 별도 {res['n_sep']}), "
        f"unit={res['detected_unit']}, pages={res['total_pages']}"
    )
    print(f"기록: {idx_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="curate_index_claude",
        description="Claude 큐레이션 인덱싱 (개발 PC 전용, 로컬 임베딩만).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("validate", help="notes 초안을 PDF 본문/규칙으로 검증")
    pv.add_argument("notes_json", help="notes.json 경로")
    pv.set_defaults(func=cmd_validate)

    pd = sub.add_parser("diff", help="현재 index.json 과 초안 비교")
    pd.add_argument("notes_json", help="notes.json 경로")
    pd.set_defaults(func=cmd_diff)

    pa = sub.add_parser("apply", help="검증 통과 시 index.json 재생성(로컬 임베딩)")
    pa.add_argument("notes_json", help="notes.json 경로")
    pa.add_argument("--yes", action="store_true", help="실제 재인덱싱 수행(비대화형)")
    pa.set_defaults(func=cmd_apply)

    pau = sub.add_parser("auto", help="기존 notes+서브헤더로 경계 재계산 초안 자동 생성(리뷰용)")
    pau.add_argument("company")
    pau.add_argument("period")
    pau.set_defaults(func=cmd_auto)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
