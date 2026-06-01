"""
collect_dart.py — DART OpenAPI 자동수집 스캐폴드 (4대 금융지주 주석 비교 에이전트 PoC)

목적
----
DART OpenAPI(opendart.fss.or.kr)에서 4대 금융지주(신한/KB/하나/우리)의
공시 데이터를 회사 × 기간 단위로 수집해, 기존 라이브러리 레이아웃
(storage/library/{회사}/{period}/) 과 호환되게 로컬 저장한다.

수집 항목(회사 × 기간):
  1) corpCode.xml  → corp_code 매핑(corp_codes.json 캐시)
  2) list.json     → 정기공시 목록에서 대상 보고서 rcept_no/report_nm/rcept_dt
  3) fnlttSinglAcntAll.json → 구조화 재무제표 → fs_structured.json
  4) fnlttXbrl.xml → 재무제표 원본 XBRL ZIP → xbrl/ 해제
  5) document.xml  → 제출원문 묶음 → source/
  + meta.json

설계 정직성 — 표시용 PDF 한계 (중요)
------------------------------------
DART OpenAPI 의 document.xml 은 "제출원문 묶음"(XML 등)을 반환하며,
뷰어용 깔끔한 PDF(예: 분기연결검토보고서)를 직접 보장하지 않는다.
따라서 본 수집기는 "구조화데이터 + XBRL 확보"를 1차 목적으로 하고,
document.xml 이 주는 원문은 그대로 source/ 에 저장한다.
표시용 report.pdf 를 자동 확보하지 못하면, 기존 수동 PDF 업로드 경로
(main.py 의 /api/library/upload → report.pdf)를 그대로 사용한다.
수집기는 main.py 가 인덱싱에 쓰는 report.pdf / index.json 을 건드리지 않는다.

보안
----
- DART_API_KEY 는 SECRET. .env 에서만 읽는다(os.getenv / 내장 5줄 파서).
  코드 하드코딩 금지. 로그/드라이런 출력에는 항상 **** 로 마스킹.
- 수집 원문은 로컬 저장만. (DART 는 공개데이터 수집이라 외부반출 위배 아님)

AI/LLM 미사용. 단위 환산 없음. DB 없음(파일 저장). Windows 11 / PowerShell 기준.

사용 (소유자, 업무망에서 키 주입 후):
    # 1) .env 에 키 1줄 추가:  DART_API_KEY=발급받은40자리키
    # 2) 드라이런(키 없이도 동작, 라이브 호출 안 함):
    python collect_dart.py --companies 신한 KB 하나 우리 --year 2025 --reprt 11014 --dry-run
    # 3) 라이브 수집(키 필요):
    python collect_dart.py --companies 신한 KB 하나 우리 --year 2025 --reprt 11014

오프라인 자체검증(키 불필요):
    python collect_dart.py --self-test
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

# httpx 는 라이브 수집에만 필요. import 실패해도 드라이런/셀프테스트는 동작해야 함.
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

# OpenDartReader 는 검토보고서(표시용 PDF) 첨부 수집에만 필요.
# import 실패(미설치)해도 드라이런/셀프테스트/기존 수집(보고서·XBRL·fnltt)은 동작해야 함.
try:
    import OpenDartReader  # noqa: N816 (외부 패키지명 그대로)
    _HAS_OPENDART = True
except ImportError:
    _HAS_OPENDART = False


# ----------------------------------------------------------------------------
# 설정 상수
# ----------------------------------------------------------------------------
DART_BASE = "https://opendart.fss.or.kr/api"

# 저장 루트 — main.py 와 동일 위치(상대경로 ./storage/library). 스크립트 위치 기준 절대경로로 고정.
SCRIPT_DIR = Path(__file__).resolve().parent
STORAGE_ROOT = SCRIPT_DIR / "storage"
LIBRARY_ROOT = STORAGE_ROOT / "library"
CORP_CODE_CACHE = STORAGE_ROOT / "corp_codes.json"

# DART corp_name(공식 회사명) → 내부 라이브러리 표기 매핑.
# 라이브러리는 신한/KB/하나/우리 로 정규화(main.py VALID_COMPANIES 와 일치).
COMPANY_MAP: Dict[str, str] = {
    "신한지주": "신한",
    "KB금융": "KB",        # DART 공식 corp_name 은 'KB금융'( 'KB금융지주' 아님 )
    "하나금융지주": "하나",
    "우리금융지주": "우리",
}
# 내부표기 → DART 공식 corp_name (corpCode.xml 매칭용)
INTERNAL_TO_CORP_NAME: Dict[str, str] = {v: k for k, v in COMPANY_MAP.items()}

# 확인된 시드/폴백 corp_code (라이브 corpCode.xml 로 4사 전부 확인 — 2026-05-27).
# 매칭이 실패해도 이 값으로 채워 4사 누락을 방지.
SEED_CORP_CODES: Dict[str, str] = {
    "신한": "00382199",
    "KB": "00688996",
    "하나": "00547583",
    "우리": "01350869",
}

# reprt_code ↔ period 매핑 (DART 정기보고서 코드)
REPRT_TO_PERIOD_SUFFIX: Dict[str, str] = {
    "11013": "Q1",  # 1분기보고서
    "11012": "Q2",  # 반기보고서
    "11014": "Q3",  # 3분기보고서
    "11011": "FY",  # 사업보고서
}

# report_nm 의 결산기 마커("(YYYY.MM)")로 대상 보고서를 정밀 선택.
#   11013→03(1분기) 11012→06(반기) 11014→09(3분기) 11011→12(사업).
REPRT_TO_PERIOD_MARK: Dict[str, str] = {
    "11013": "03", "11012": "06", "11014": "09", "11011": "12",
}


def list_date_window(year: int, reprt_code: str):
    """list.json 의 bgn_de/end_de(접수일 범위). list.json 은 bsns_year 미지원이라
    해당 보고서가 실제 접수되는 기간을 범위로 준다(정정 포함 여유).
    """
    if reprt_code == "11013":   # 1분기(3월결산) — 5월경 접수
        return f"{year}0401", f"{year}0831"
    if reprt_code == "11012":   # 반기(6월) — 8월경
        return f"{year}0701", f"{year}1031"
    if reprt_code == "11014":   # 3분기(9월) — 11월경
        return f"{year}1001", f"{year+1}0228"
    if reprt_code == "11011":   # 사업(12월) — 익년 3월경
        return f"{year+1}0101", f"{year+1}0630"
    return f"{year}0101", f"{year+1}0630"


VALID_COMPANIES = set(COMPANY_MAP.values())  # {신한, KB, 하나, 우리}


# ----------------------------------------------------------------------------
# .env 로딩 / 키 마스킹
# ----------------------------------------------------------------------------
def load_env_file(env_path: Optional[Path] = None) -> None:
    """.env 파일을 읽어 os.environ 에 주입(이미 설정된 환경변수는 보존).

    python-dotenv 의존을 피하기 위한 내장 5줄 파서. KEY=VALUE 형식만 처리하고,
    빈 줄/`#` 주석은 무시한다. 값에 = 가 있어도 첫 = 만 분리.
    """
    env_path = env_path or (SCRIPT_DIR / ".env")
    if not env_path.exists():
        return
    try:
        # utf-8-sig: PowerShell 5.1 의 Set-Content -Encoding utf8 이 붙이는 BOM 을 제거.
        # (BOM 이 있으면 첫 키가 '﻿DART_API_KEY' 로 읽혀 미설정으로 오인됨)
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # 셸/CI 에서 이미 주입한 값이 우선(보안: 파일 < 환경)
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as e:
        # [load_env_file] .env 읽기 실패 — 경로/권한 문제. 키 없이 진행 가능하므로 경고만.
        print(f"[load_env_file] .env 읽기 실패 - {env_path}: {e}", file=sys.stderr)


def get_api_key() -> Optional[str]:
    """DART_API_KEY 를 환경에서 읽음. .env 선로딩. 없으면 None."""
    load_env_file()
    key = os.getenv("DART_API_KEY")
    return key.strip() if key else None


def mask_key(key: Optional[str]) -> str:
    """로그/출력용 키 마스킹. 절대 원문 노출 금지."""
    if not key:
        return "****(미설정)"
    # 길이 정보만 노출해 오타 판별을 돕되 값은 가린다.
    return "****" + f"(len={len(key)})"


# ----------------------------------------------------------------------------
# 기간/경로 유틸
# ----------------------------------------------------------------------------
def period_from_reprt(year: int, reprt_code: str) -> str:
    """(year, reprt_code) → 라이브러리 period 표기. 예: (2025,'11014')→'2025Q3'."""
    suffix = REPRT_TO_PERIOD_SUFFIX.get(reprt_code)
    if suffix is None:
        raise ValueError(
            f"[period_from_reprt] 알 수 없는 reprt_code - {reprt_code} "
            f"(허용: {', '.join(REPRT_TO_PERIOD_SUFFIX)})"
        )
    return f"{year}{suffix}"


def entry_dir(company: str, period: str) -> Path:
    """라이브러리 저장 디렉토리. main.py 와 동일 규칙(정규화 회사명/period)."""
    if company not in VALID_COMPANIES:
        raise ValueError(f"[entry_dir] 알 수 없는 회사 - {company} (허용: {VALID_COMPANIES})")
    return LIBRARY_ROOT / company / period


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# corpCode.xml 파싱 — 회사명 → corp_code 매핑
# ----------------------------------------------------------------------------
def parse_corp_code_xml(xml_bytes: bytes) -> Dict[str, str]:
    """corpCode.xml(전체 고유번호) 바이트에서 4사 corp_code 를 추출.

    DART corpCode.xml 스키마: <result><list><corp_code/><corp_name/>... </list>...</result>
    회사명이 INTERNAL_TO_CORP_NAME 에 매칭되면 내부표기 키로 담는다.

    Returns: {내부표기: corp_code}  (예: {"신한": "00382199", ...})
    """
    def _norm(s: str) -> str:
        # (주)/주식회사/공백 변형 흡수. 정규화 후 '동등' 비교라 부분일치 과매칭(신한지주증권)은 배제.
        return re.sub(r"\s+|\(주\)|주식회사", "", s or "")

    wanted_norm = {_norm(name): internal for internal, name in INTERNAL_TO_CORP_NAME.items()}
    found: Dict[str, str] = {}
    root = ET.fromstring(xml_bytes)
    for item in root.iter("list"):
        corp_name_el = item.find("corp_code")
        name_el = item.find("corp_name")
        if name_el is None or corp_name_el is None:
            continue
        internal = wanted_norm.get(_norm((name_el.text or "").strip()))
        if internal:
            found[internal] = (corp_name_el.text or "").strip()
    return found


def resolve_corp_codes(found: Dict[str, str]) -> Dict[str, str]:
    """매칭 결과에 시드 폴백 적용. 신한 미매칭 시 SEED 값 사용."""
    resolved = dict(found)
    for internal, seed in SEED_CORP_CODES.items():
        resolved.setdefault(internal, seed)
    # 미해결 회사 경고 — corpCode.xml 공식명이 가정과 다르면 조용히 누락될 수 있음.
    unresolved = [c for c in VALID_COMPANIES if c not in resolved]
    if unresolved:
        print(f"[resolve_corp_codes] corp_code 미해결 - {unresolved}: "
              f"corpCode.xml 의 실제 corp_name 을 확인해 COMPANY_MAP 보정 필요(시드 없음)",
              file=sys.stderr)
    return resolved


# ----------------------------------------------------------------------------
# fnlttSinglAcntAll.json 파싱 — 구조화 재무제표
# ----------------------------------------------------------------------------
def parse_fnltt_single_acnt(resp: dict) -> dict:
    """fnlttSinglAcntAll.json 응답을 fs_structured.json 구조로 정규화.

    공식 응답 스키마(요지):
      {
        "status": "000",          # "000"=정상, 그 외 오류
        "message": "정상",
        "list": [
          {
            "rcept_no": "...", "bsns_year": "2025", "corp_code": "...",
            "sj_div": "BS|IS|CIS|CF|SCE",   # 재무제표 종류
            "sj_nm": "재무상태표",
            "account_id": "...", "account_nm": "유동자산", "account_detail": "-",
            "fs_div": "CFS|OFS",            # 연결/별도
            "fs_nm": "연결재무제표",
            "thstrm_nm": "제 N 기", "thstrm_amount": "123456",
            "frmtrm_nm": "...", "frmtrm_amount": "...",
            "bfefrmtrm_amount": "...", "ord": "1", "currency": "KRW", ...
          }, ...
        ]
      }

    단위 환산 없음 — thstrm_amount 등 금액은 원문 문자열 그대로 보존.
    오류 status(≠"000")는 예외로 알림.
    """
    status = str(resp.get("status", ""))
    message = resp.get("message", "")
    if status != "000":
        raise ValueError(
            f"[parse_fnltt_single_acnt] DART 응답 오류 - status={status} message={message}"
        )

    # 보존할 필드(공식 명칭 그대로). 원문 보존 원칙 — 가공/환산 금지.
    keep_fields = (
        "sj_div", "sj_nm", "fs_div", "fs_nm",
        "account_id", "account_nm", "account_detail",
        "thstrm_nm", "thstrm_amount",
        "frmtrm_nm", "frmtrm_amount", "frmtrm_q_amount",
        "bfefrmtrm_nm", "bfefrmtrm_amount",
        "ord", "currency",
    )
    accounts: List[dict] = []
    for row in resp.get("list", []):
        accounts.append({k: row.get(k) for k in keep_fields if k in row})

    # fs_div / sj_div 별 분포 요약(검증·디버깅용. 환산 아님 — 단순 카운트).
    fs_divs = sorted({a.get("fs_div") for a in accounts if a.get("fs_div")})
    sj_divs = sorted({a.get("sj_div") for a in accounts if a.get("sj_div")})

    return {
        "status": status,
        "message": message,
        "account_count": len(accounts),
        "fs_div_present": fs_divs,
        "sj_div_present": sj_divs,
        "accounts": accounts,
    }


# ----------------------------------------------------------------------------
# XBRL ZIP 파싱 — _lab-ko.xml 한국어 라벨 추출(검증용)
# ----------------------------------------------------------------------------
# XBRL linkbase 네임스페이스
_NS_LINK = "http://www.xbrl.org/2003/linkbase"
_NS_XLINK = "http://www.w3.org/1999/xlink"
# 표준 라벨 role(다른 role: terseLabel, totalLabel, periodStart/End 등 제외해 대표 라벨만)
_STD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"


def extract_xbrl_labels(lab_ko_bytes: bytes, limit: Optional[int] = None) -> List[str]:
    """XBRL _lab-ko.xml 에서 한국어 표준 라벨 텍스트를 추출.

    <link:label xml:lang="ko" xlink:role=".../label">보통주자본금</link:label> 형태.
    표준 label role 만 채택(중복/변형 라벨 role 제외). limit 이 있으면 앞 N 개만.

    Returns: 라벨 문자열 리스트(원문 그대로).
    """
    labels: List[str] = []
    role_attr = f"{{{_NS_XLINK}}}role"
    lang_attr = "{http://www.w3.org/XML/1998/namespace}lang"
    label_tag = f"{{{_NS_LINK}}}label"

    # 대용량(수 MB) XML — iterparse 로 스트리밍 파싱(메모리 절약).
    for _event, elem in ET.iterparse(io.BytesIO(lab_ko_bytes), events=("end",)):
        if elem.tag != label_tag:
            continue
        if elem.get(role_attr) == _STD_LABEL_ROLE and elem.get(lang_attr) == "ko":
            text = (elem.text or "").strip()
            if text:
                labels.append(text)
        elem.clear()  # 처리 후 즉시 비워 메모리 누수 방지
        if limit is not None and len(labels) >= limit:
            break
    return labels


def unzip_to(zip_bytes: bytes, dest_dir: Path) -> List[str]:
    """ZIP 바이트를 dest_dir 에 해제. 경로탈출(zip slip) 방지. 해제 파일명 리스트 반환."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    extracted: List[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.namelist():
            # 보안: 절대경로/.. 로 dest 밖으로 빠지는 항목 차단(zip slip).
            # str startswith 는 형제 디렉터리(xbrlEVIL)로 우회 가능 → relative_to 로 엄격 검사.
            target = (dest_dir / member).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                print(f"[unzip_to] 경로탈출 차단 - {member}", file=sys.stderr)
                continue
            zf.extract(member, dest_dir)
            extracted.append(member)
    return extracted


def find_lab_ko_in_zip(zip_path: Path) -> Optional[bytes]:
    """ZIP 파일에서 *_lab-ko.xml 엔트리 바이트를 반환(오프라인 검증용)."""
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("_lab-ko.xml"):
                return zf.read(name)
    return None


# ----------------------------------------------------------------------------
# source_type 판정 (보고서명 기반)
# ----------------------------------------------------------------------------
def classify_source_type(report_nm: str) -> str:
    """보고서명으로 full_report / slim 판정.

    사업보고서·분기보고서·반기보고서 = full_report(전체 본문+주석).
    검토보고서 = slim(주석 중심). 기타는 보수적으로 full_report.
    """
    nm = report_nm or ""
    if "검토보고서" in nm:
        return "slim"
    if any(k in nm for k in ("사업보고서", "분기보고서", "반기보고서")):
        return "full_report"
    return "full_report"


# ----------------------------------------------------------------------------
# 검토보고서 첨부(표시용 PDF) 수집 — OpenDartReader 2단계 경로
# ----------------------------------------------------------------------------
# attach_docs 의 title 후보 선택 우선순위. "연결검토보고서" 가 가장 구체적이라 1순위,
# 그 다음 "검토보고서"(연결/별도 표기 변형 흡수), 마지막으로 "감사보고서"(분기검토 대신
# 첨부될 수 있는 케이스 대비). 회사별 title 변형(KB/하나/우리)을 키워드로 흡수한다.
_REVIEW_TITLE_PRIORITY: Tuple[str, ...] = ("연결검토보고서", "검토보고서", "감사보고서")

# 분기/반기/사업보고서 "본문" 문서 선택 우선순위. 키워드가 본문 보고서명만
# 포함하므로 검토/감사 첨부와 겹치지 않는다(예: "분기검토보고서"는 "분기보고서"를
# 부분문자열로 포함하지 않음 → review 문서 오선택 없음).
# 참고: 이 보고서명은 DART 원본 파일명 매칭용(불변). 연결/별도(CFS/OFS) 구분은
# 문서 종류가 아니라 fs_structured.json 의 by_fs_div 와 주석 note.fs_div 로 한다.
_REPORT_TITLE_PRIORITY: Tuple[str, ...] = ("분기보고서", "반기보고서", "사업보고서")

# 매직넘버: PDF 파일 시그니처. 첫 5바이트가 이것이면 표시용 PDF 로 확정.
_PDF_MAGIC = b"%PDF-"


def _normalize_doc_rows(docs) -> List[Tuple[str, str]]:
    """attach_docs 반환값(DataFrame 또는 list[dict])을 [(title, url)] 시퀀스로 정규화.

    오프라인 픽스처는 list, 라이브는 pandas DataFrame 을 준다 — 양쪽을 흡수한다.
    """
    rows: List[Tuple[str, str]] = []
    if docs is None:
        return rows
    if hasattr(docs, "iterrows"):  # pandas DataFrame
        if docs.empty:
            return rows
        for _idx, row in docs.iterrows():
            rows.append((str(row.get("title", "")), str(row.get("url", ""))))
    else:  # list[dict] 또는 유사 시퀀스
        for r in docs:
            rows.append((str(r.get("title", "")), str(r.get("url", ""))))
    return rows


def _pick_doc_by_priority(docs, priority: Tuple[str, ...]) -> Optional[dict]:
    """정규화된 문서 행에서 우선순위 키워드를 순서대로 적용해 첫 매칭 1건 선택.

    동일 우선순위 내에서는 먼저 등장한 행을 택한다. 없으면 None.
    """
    rows = _normalize_doc_rows(docs)
    for keyword in priority:
        for title, url in rows:
            if keyword in title and url:
                return {"title": title, "url": url}
    return None


def pick_review_doc(docs) -> Optional[dict]:
    """attach_docs 결과에서 검토보고서 후보 1건 선택.

    우선순위(_REVIEW_TITLE_PRIORITY): 연결검토보고서 > 검토보고서 > 감사보고서.
    없으면 None(정상 — review 미수집).
    """
    return _pick_doc_by_priority(docs, _REVIEW_TITLE_PRIORITY)


def pick_report_pdf(files: dict) -> Optional[Tuple[str, str]]:
    """attach_files(rcept_no) 결과 dict{파일명: url}에서 본문 보고서 PDF 1건 선택.

    본문 보고서(예: '[하나금융지주]분기보고서(2025.11.14).pdf')는 attach_docs(첨부문서
    =검토/감사보고서)에는 없고 attach_files(rcept_no)가 직접 반환한다(라이브 확인).
    같이 오는 XBRL 원문(.zip)은 표시용 PDF 가 아니므로 .pdf 만 후보로 둔다.
    우선순위: 분기>반기>사업보고서 키워드를 파일명에 포함한 .pdf, 없으면 첫 .pdf.
    후보 없으면 None.

    Returns: (파일명, url) 또는 None.
    """
    if not files:
        return None
    pdfs = [(name, url) for name, url in files.items()
            if str(name).lower().endswith(".pdf")]
    if not pdfs:
        return None
    for keyword in _REPORT_TITLE_PRIORITY:
        for name, url in pdfs:
            if keyword in name:
                return (name, url)
    return pdfs[0]


def is_pdf_bytes(head: bytes) -> bool:
    """파일 첫 바이트가 PDF 매직넘버(%PDF-)로 시작하는지 판정."""
    return bool(head) and head[:5] == _PDF_MAGIC


def _read_head(path: Path, n: int = 5) -> bytes:
    """파일 앞 n 바이트만 읽음(매직넘버 판정용). 실패 시 빈 바이트."""
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


# Windows 파일명 금지문자만 치환. 브래킷/괄호/점/한글은 보존(원본 파일명 유지 — R2).
_WIN_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')


def safe_original_name(name: str, fallback: str) -> str:
    """DART 원본 파일명을 Windows 안전 파일명으로(금지문자만 치환, 나머지 보존).
    빈 값이면 fallback 사용. 경로 구분자 제거로 디렉터리 탈출 방지."""
    base = (name or "").strip().replace("\x00", "")
    base = base.replace("/", "_").replace("\\", "_")
    base = _WIN_FORBIDDEN.sub("_", base)
    base = base.strip(". ")  # Windows 는 끝 점/공백 불가
    return base or fallback


# DART 뷰어 PDF(pdf.do)는 OpenDartReader.download 가 빈 파일을 쓰는 경우가 있어
# (라이브 확인) httpx 로 직접 GET. Referer/UA 헤더 필요. 성공 시 True.
def download_attachment(url: str, dest_path: Path) -> bool:
    """첨부 PDF URL을 직접 다운로드(헤더 포함). 비어있지 않은 200 응답만 저장."""
    if not _HAS_HTTPX or not url:
        return False
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://dart.fss.or.kr/"}
    for attempt in range(3):
        try:
            with httpx.Client(follow_redirects=True, timeout=60.0, headers=headers) as cl:
                r = cl.get(url)
            if r.status_code == 200 and r.content:
                dest_path.write_bytes(r.content)
                return dest_path.exists() and dest_path.stat().st_size > 0
        except Exception as e:
            # 보안: URL 에 키가 없지만 메시지엔 타입만 남긴다.
            print(f"[download_attachment] 시도{attempt+1} 실패({type(e).__name__})", file=sys.stderr)
        time.sleep(1.5 * (attempt + 1))
    return False


def fetch_review_attachment(odr, rcept_no: str, dest_dir: Path) -> Optional[dict]:
    """분기검토/연결검토보고서 첨부(표시용 PDF)를 OpenDartReader 2단계로 수집.

    경로(라이브 검증됨):
      1) odr.attach_docs(rcept_no) → DataFrame[title,url]. 검토보고서 후보 선택.
      2) odr.attach_files(후보 url) → dict{파일명: pdf_url}. .pdf 키 우선.
      3) odr.download(pdf_url, dest/"review.pdf").
    저장 후 매직넘버(%PDF-) 검증. PDF 가 아니면 review.raw 로 보존(display_ok=False).

    보안: OpenDartReader 내부 예외 메시지/URL 에는 키가 섞일 수 있으므로
    예외는 type(e).__name__ 만 로깅한다. 모든 실패는 경고 후 None 반환 —
    기존 분기보고서/XBRL/fnltt 수집 흐름을 절대 깨지 않는다.

    Args:
        odr: OpenDartReader 인스턴스(run_live 에서 1회 생성).
        rcept_no: 대상 보고서 접수번호.
        dest_dir: 저장 디렉토리(회사×기간 셀).
    Returns:
        성공 시 {doc_type, file, filename_dart, source_type, display_ok, mime?} dict,
        후보 없음/실패 시 None.
    """
    if odr is None or not rcept_no:
        return None
    try:
        # 1) 첨부문서 목록 → 검토보고서 후보 선택
        docs = odr.attach_docs(rcept_no)
        cand = pick_review_doc(docs)
        if cand is None:
            print(f"[fetch_review_attachment] rcept_no={rcept_no} 검토보고서 후보 없음 "
                  f"(review 미수집, 정상)", file=sys.stderr)
            return None

        # 2) 후보 문서의 다운로드 첨부파일(dict{파일명: url}) → pdf_url 선택.
        #    attach_files 는 내부 2-요청 구조라 DART 레이트리밋 시 간헐적으로 빈 dict 를
        #    반환한다(라이브 확인). 짧은 백오프로 최대 3회 재시도.
        files = {}
        for attempt in range(3):
            files = odr.attach_files(cand["url"]) or {}
            if files:
                break
            time.sleep(1.5 * (attempt + 1))  # 1.5s, 3.0s 백오프
        if not files:
            print(f"[fetch_review_attachment] rcept_no={rcept_no} 첨부파일 없음 "
                  f"(title={cand['title']}, 3회 재시도 후) — review 스킵", file=sys.stderr)
            return None
        # .pdf 키 우선, 없으면 첫 항목.
        pdf_name = next((k for k in files if k.lower().endswith(".pdf")), None)
        if pdf_name is None:
            pdf_name = next(iter(files))
        pdf_url = files[pdf_name]

        # 3) 다운로드 → R2: DART 원본 파일명 그대로 저장(수정/삭제 금지). 재시도.
        dest_dir.mkdir(parents=True, exist_ok=True)
        orig_name = safe_original_name(pdf_name, "review_original.pdf")
        orig_path = dest_dir / orig_name
        # OpenDartReader.download 는 pdf.do URL 에서 빈 파일을 쓰는 사례가 있어 직접 GET 사용.
        if not download_attachment(pdf_url, orig_path):
            print(f"[fetch_review_attachment] rcept_no={rcept_no} 다운로드 실패 "
                  f"(title={cand['title']}, 3회 재시도 후) — review 스킵", file=sys.stderr)
            return None

        # 4) 매직넘버 검증 — PDF 면 앱 내부용 review.pdf 로 복제(원본 보존), 아니면 폴백
        head = _read_head(orig_path, 5)
        if is_pdf_bytes(head):
            out_pdf = dest_dir / "review.pdf"
            shutil.copy2(orig_path, out_pdf)  # 원본 미삭제 — review.pdf 는 복제본
            return {
                "doc_type": "review",
                "file": "review.pdf",
                "filename_original": orig_name,
                "filename_dart": cand["title"],
                "source_type": "slim",
                "display_ok": True,
            }
        # 비PDF — 표시용으로 못 씀. 원본은 그대로 보존(review.pdf 미생성).
        mime = "application/zip" if head[:2] == b"PK" else "application/octet-stream"
        print(f"[fetch_review_attachment] rcept_no={rcept_no} 비PDF 첨부 - "
              f"원본({orig_name}) 보존, review.pdf 미생성(첫바이트={head!r}, mime={mime})", file=sys.stderr)
        return {
            "doc_type": "review",
            "file": orig_name,
            "filename_original": orig_name,
            "filename_dart": cand["title"],
            "source_type": "slim",
            "display_ok": False,
            "mime": mime,
        }
    except Exception as e:
        # 보안: OpenDartReader 예외 메시지는 키 포함 URL 을 노출할 수 있음 → 타입명만.
        print(f"[fetch_review_attachment] rcept_no={rcept_no} 검토보고서 수집 실패 "
              f"({type(e).__name__}) — review 스킵, 기존 수집은 계속.", file=sys.stderr)
        return None


def fetch_report_attachment(odr, rcept_no: str, dest_dir: Path,
                            overwrite: bool = False) -> Optional[dict]:
    """분기/반기/사업보고서 "본문"(표시용 PDF)을 OpenDartReader 2단계로 수집.

    경로(라이브 확인) — 본문 보고서는 attach_docs(첨부=검토/감사)에 없고
    attach_files(rcept_no)가 직접 준다:
      1) odr.attach_files(rcept_no) → dict{파일명: url}. 본문 보고서 .pdf 선택(XBRL zip 제외).
      2) odr.download(pdf_url, dest/"report.pdf").
    저장 후 매직넘버(%PDF-) 검증. PDF 가 아니면 report.raw 로 격리(display_ok=False) —
    인덱싱이 깨진 report.pdf 를 집어가지 않도록.

    무클로버 계약(중요): report.pdf 가 이미 있으면 overwrite=False 인 한 절대
    덮어쓰지 않고 None 반환한다. main.py 의 수동 업로드/인덱싱(report.pdf·index.json)
    경로를 보존하기 위함. index.json 도 건드리지 않는다.

    실패는 전부 경고 후 None — 기존 분기보고서/XBRL/fnltt 수집 흐름 무회귀.

    Args:
        odr: OpenDartReader 인스턴스(run_live 에서 1회 생성).
        rcept_no: 대상 보고서 접수번호.
        dest_dir: 저장 디렉토리(회사×기간 셀).
        overwrite: True 면 기존 report.pdf 도 덮어씀(기본 False — 무클로버).
    Returns:
        성공 시 {doc_type, file, filename_dart, source_type, display_ok, mime?} dict,
        무클로버 스킵/후보 없음/실패 시 None.
    """
    if odr is None or not rcept_no:
        return None
    out_pdf = dest_dir / "report.pdf"
    if out_pdf.exists() and not overwrite:
        print(f"[fetch_report_attachment] rcept_no={rcept_no} report.pdf 이미 존재 "
              f"— 무클로버(수동 업로드/인덱싱 보존), report 수집 스킵", file=sys.stderr)
        return None
    try:
        # 1) 본문 첨부파일(dict{파일명: url}) → 본문 보고서 .pdf 선택.
        #    레이트리밋 시 빈 dict 대비 3회 재시도.
        files = {}
        for attempt in range(3):
            files = odr.attach_files(rcept_no) or {}
            if files:
                break
            time.sleep(1.5 * (attempt + 1))
        if not files:
            print(f"[fetch_report_attachment] rcept_no={rcept_no} 첨부파일 없음 "
                  f"(3회 재시도 후) — report 스킵", file=sys.stderr)
            return None
        picked = pick_report_pdf(files)
        if picked is None:
            print(f"[fetch_report_attachment] rcept_no={rcept_no} 본문 보고서 PDF 후보 없음 "
                  f"(.pdf 없음/XBRL zip 만 존재) — report 미수집", file=sys.stderr)
            return None
        pdf_name, pdf_url = picked

        # 2) 다운로드 → R2: DART 원본 파일명 그대로 저장(수정/삭제 금지). 3회 재시도.
        dest_dir.mkdir(parents=True, exist_ok=True)
        orig_name = safe_original_name(pdf_name, "report_original.pdf")
        orig_path = dest_dir / orig_name
        # OpenDartReader.download 는 pdf.do URL 에서 빈 파일을 쓰는 사례가 있어 직접 GET 사용.
        if not download_attachment(pdf_url, orig_path):
            print(f"[fetch_report_attachment] rcept_no={rcept_no} 다운로드 실패 "
                  f"(file={pdf_name}, 3회 재시도 후) — report 스킵", file=sys.stderr)
            return None

        # 3) 매직넘버 검증 — PDF 면 앱 내부용 report.pdf 로 복제(원본 보존), 아니면 폴백
        head = _read_head(orig_path, 5)
        if is_pdf_bytes(head):
            shutil.copy2(orig_path, out_pdf)  # 원본 미삭제 — report.pdf 는 복제본(인덱싱/표시용)
            return {
                "doc_type": "report",
                "file": "report.pdf",
                "filename_original": orig_name,
                "filename_dart": pdf_name,
                "source_type": classify_source_type(pdf_name),
                "display_ok": True,
            }
        # 비PDF — 인덱싱이 깨질 수 있으므로 원본만 보존(report.pdf 미생성).
        mime = "application/zip" if head[:2] == b"PK" else "application/octet-stream"
        print(f"[fetch_report_attachment] rcept_no={rcept_no} 비PDF 첨부 - "
              f"원본({orig_name}) 보존, report.pdf 미생성(첫바이트={head!r}, mime={mime})", file=sys.stderr)
        return {
            "doc_type": "report",
            "file": orig_name,
            "filename_original": orig_name,
            "filename_dart": pdf_name,
            "source_type": classify_source_type(pdf_name),
            "display_ok": False,
            "mime": mime,
        }
    except Exception as e:
        # 보안: OpenDartReader 예외 메시지는 키 포함 URL 을 노출할 수 있음 → 타입명만.
        print(f"[fetch_report_attachment] rcept_no={rcept_no} 본문 보고서 수집 실패 "
              f"({type(e).__name__}) — report 스킵, 기존 수집은 계속.", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# 라이브 수집 — 요청 계획 빌더 (dry-run 과 live 가 공유)
# ----------------------------------------------------------------------------
def build_request_plan(company: str, corp_code: str, year: int,
                       reprt_code: str, period: str, api_key: Optional[str]) -> List[dict]:
    """회사 1곳의 호출 계획(URL/파라미터)을 생성. 키는 호출에만 쓰고 출력 시 마스킹.

    Returns: [{"step","url","params"}] — params 안의 crtfc_key 는 호출용 placeholder.
    rcept_no 의존 호출(fnlttXbrl/document)은 list.json 수집 후 채워지므로 여기선 표기만.
    """
    return [
        {
            "step": "1.corpCode",
            "url": f"{DART_BASE}/corpCode.xml",
            "params": {"crtfc_key": api_key},
        },
        {
            "step": "2.list",
            "url": f"{DART_BASE}/list.json",
            "params": {"crtfc_key": api_key, "corp_code": corp_code,
                       "bsns_year": str(year), "pblntf_ty": "A"},
        },
        {
            "step": "3.fnlttSinglAcntAll(CFS)",
            "url": f"{DART_BASE}/fnlttSinglAcntAll.json",
            "params": {"crtfc_key": api_key, "corp_code": corp_code,
                       "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": "CFS"},
        },
        {
            "step": "4.fnlttXbrl",
            "url": f"{DART_BASE}/fnlttXbrl.xml",
            "params": {"crtfc_key": api_key, "rcept_no": "<list.json 에서 확보>",
                       "reprt_code": reprt_code},
        },
        {
            "step": "5.document",
            "url": f"{DART_BASE}/document.xml",
            "params": {"crtfc_key": api_key, "rcept_no": "<list.json 에서 확보>"},
        },
    ]


def _masked_params(params: dict) -> dict:
    """파라미터 dict 에서 crtfc_key 를 마스킹한 사본 반환(출력 전용)."""
    out = dict(params)
    if "crtfc_key" in out:
        out["crtfc_key"] = mask_key(out["crtfc_key"])
    return out


# ----------------------------------------------------------------------------
# 드라이런
# ----------------------------------------------------------------------------
def run_dry_run(companies: List[str], year: int, reprt_code: str,
                collect_report: bool = False) -> None:
    """키 없이 동작 — 호출 계획·corp_code·저장경로만 출력. 라이브 호출 없음."""
    api_key = get_api_key()  # 있으면 마스킹만, 없어도 됨
    period = period_from_reprt(year, reprt_code)

    print("=" * 70)
    print("DART 수집 DRY-RUN (라이브 호출 없음)")
    print("=" * 70)
    print(f"  API 키        : {mask_key(api_key)}")
    print(f"  연도/reprt    : {year} / {reprt_code}  →  period={period}")
    print(f"  대상 회사     : {', '.join(companies)}")
    print(f"  저장 루트     : {LIBRARY_ROOT}")
    print(f"  corp_code 캐시: {CORP_CODE_CACHE}")
    print("-" * 70)

    # corp_code: 캐시 있으면 사용, 없으면 시드만 표기(나머지는 라이브 corpCode.xml 에서 확보).
    corp_codes = resolve_corp_codes(_load_cached_corp_codes())
    print("  corp_code (시드/캐시 기준; 미확보분은 라이브 corpCode.xml 에서 매칭):")
    for c in companies:
        code = corp_codes.get(c, "<corpCode.xml 매칭 필요>")
        seed_note = " (시드)" if c in SEED_CORP_CODES and code == SEED_CORP_CODES[c] else ""
        print(f"    - {c:<3} : {code}{seed_note}")
    print("-" * 70)

    for c in companies:
        corp_code = corp_codes.get(c, "<TBD>")
        d = entry_dir(c, period)
        print(f"\n[{c}] period={period}  corp_code={corp_code}")
        print(f"  저장 경로:")
        print(f"    {d}\\")
        print(f"      source/             (document.xml 원문 묶음)")
        print(f"      xbrl/               (fnlttXbrl ZIP 해제)")
        print(f"      fs_structured.json  (fnlttSinglAcntAll 결과)")
        print(f"      meta.json")
        print(f"      (report.pdf 는 표시용 — 미확보 시 수동 업로드 유지)")
        print(f"  호출 계획:")
        for req in build_request_plan(c, corp_code, year, reprt_code, period, api_key):
            print(f"    {req['step']:<28} GET {req['url']}")
            print(f"      params={_masked_params(req['params'])}")
        # 6단계: 검토보고서 첨부(OpenDartReader 2단계). 키는 odr 내부 사용(여기선 미출력).
        odr_state = "사용 가능" if _HAS_OPENDART else "미설치(스킵 — pip install \"OpenDartReader>=0.2,<0.3\")"
        print(f"    {'6.review(첨부)':<28} attach_docs→attach_files→download review.pdf")
        print(f"      OpenDartReader={odr_state}, 후보=연결검토보고서>검토보고서, "
              f"저장={entry_dir(c, period)}\\review.pdf (매직넘버 %PDF- 검증)")
        if collect_report:
            print(f"    {'7.report(본문 PDF)':<28} attach_files(rcept_no)→.pdf 선택→download report.pdf")
            print(f"      OpenDartReader={odr_state}, 후보=분기>반기>사업보고서 .pdf(XBRL zip 배제), "
                  f"무클로버(기존 보존), 저장={entry_dir(c, period)}\\report.pdf (매직넘버 %PDF- 검증)")

    print("\n" + "=" * 70)
    print("DRY-RUN 종료 — 키를 .env(DART_API_KEY)에 넣고 --dry-run 없이 실행하면 라이브 수집.")
    print("=" * 70)


def _load_cached_corp_codes() -> Dict[str, str]:
    if CORP_CODE_CACHE.exists():
        try:
            return json.loads(CORP_CODE_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


# ----------------------------------------------------------------------------
# 라이브 수집 (소유자가 업무망에서 실행)
# ----------------------------------------------------------------------------
def _http_get(client, url: str, params: dict) -> "httpx.Response":
    """GET 래퍼 — 에러 메시지 3요소(어디서/무엇이/왜) 포함. 키는 절대 미출력."""
    try:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r
    except Exception as e:
        # 보안: httpx 예외 문자열(str(e))은 crtfc_key 가 포함된 전체 URL 을 노출할 수 있음.
        # → 예외 타입 + 상태코드만 사용하고 원본 메시지·params 는 출력하지 않는다.
        status = getattr(getattr(e, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status is not None else type(e).__name__
        # from None: 원본 예외 체인을 끊어 트레이스백에 키 포함 URL 이 노출되지 않게 함.
        raise RuntimeError(f"[_http_get] DART 호출 실패 - {url} ({detail})") from None


def fetch_and_store_corp_codes(client, api_key: str) -> Dict[str, str]:
    """corpCode.xml(ZIP) 다운로드 → 파싱 → 시드 폴백 → 캐시 저장."""
    r = _http_get(client, f"{DART_BASE}/corpCode.xml", {"crtfc_key": api_key})
    # 응답은 ZIP(내부 CORPCODE.xml). 압축 해제 후 파싱.
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if not xml_name:
            raise RuntimeError("[fetch_and_store_corp_codes] corpCode ZIP 에 XML 없음")
        xml_bytes = zf.read(xml_name)
    resolved = resolve_corp_codes(parse_corp_code_xml(xml_bytes))
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    CORP_CODE_CACHE.write_text(json.dumps(resolved, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return resolved


def pick_target_report(list_resp: dict, reprt_code: str, year: int) -> Optional[dict]:
    """list.json 에서 대상 정기보고서 1건 선택.

    DART list.json: {"status","message","list":[{rcept_no,report_nm,rcept_dt,...}]}.
    report_nm 으로 reprt_code 에 해당하는 보고서를 매칭(분기/반기/사업).
    rcept_dt 최신 1건 선택(정정 반영). 못 찾으면 None.
    """
    if str(list_resp.get("status")) != "000":
        return None
    # reprt_code → 보고서명 키워드
    nm_key = {"11013": "분기보고서", "11012": "반기보고서",
              "11014": "분기보고서", "11011": "사업보고서"}.get(reprt_code, "")
    mark = REPRT_TO_PERIOD_MARK.get(reprt_code, "")
    period_tag = f"{year}.{mark}"  # 예: '2025.09' — report_nm '(2025.09)' 와 매칭
    items = list_resp.get("list", [])
    # 1순위: 보고서명 + 결산기 마커 동시 일치(1분기/3분기 혼동 방지)
    cands = [it for it in items
             if nm_key in (it.get("report_nm") or "") and period_tag in (it.get("report_nm") or "")]
    # 2순위(마커 표기가 다른 경우): 보고서명 키워드만
    if not cands:
        cands = [it for it in items if nm_key in (it.get("report_nm") or "")]
    if not cands:
        return None
    # rcept_dt(YYYYMMDD) 최신 우선(정정 반영)
    cands.sort(key=lambda it: it.get("rcept_dt", ""), reverse=True)
    return cands[0]


def collect_company(client, api_key: str, company: str, corp_code: str,
                    year: int, reprt_code: str, period: str, odr=None,
                    collect_report: bool = False) -> dict:
    """회사 1곳 라이브 수집. fs_structured.json/xbrl/source/meta.json 저장.

    index.json 은 절대 건드리지 않는다(인덱싱 경로 보존). report.pdf 는 기본적으로
    건드리지 않으나, collect_report=True 면 본문 보고서 PDF 를 "무클로버"로 수집한다
    (기존 report.pdf 가 있으면 스킵 — 수동 업로드 보존).

    Args:
        odr: OpenDartReader 인스턴스(run_live 에서 1회 생성). 검토보고서(review.pdf)
             및 본문 보고서(report.pdf) 첨부 수집에 사용. None 이면 스킵.
        collect_report: True 면 본문 보고서 PDF(report.pdf) 자동수집 시도(무클로버).
    Returns: meta dict.
    """
    d = entry_dir(company, period)
    d.mkdir(parents=True, exist_ok=True)

    # 2) 공시검색 → 대상 보고서. list.json 은 bsns_year 미지원 → 접수일 범위(bgn_de/end_de) 사용.
    bgn_de, end_de = list_date_window(year, reprt_code)
    list_resp = _http_get(client, f"{DART_BASE}/list.json",
                          {"crtfc_key": api_key, "corp_code": corp_code,
                           "bgn_de": bgn_de, "end_de": end_de,
                           "pblntf_ty": "A", "page_count": "100"}).json()
    target = pick_target_report(list_resp, reprt_code, year)
    rcept_no = target.get("rcept_no") if target else None
    report_nm = target.get("report_nm") if target else None
    rcept_dt = target.get("rcept_dt") if target else None

    # 3) 단일회사 전체 재무제표(연결 CFS 우선; 별도 OFS 도 시도)
    fs_divs_collected = []
    fs_payload = {"status": None, "message": None, "by_fs_div": {}}
    for fs_div in ("CFS", "OFS"):
        try:
            resp = _http_get(client, f"{DART_BASE}/fnlttSinglAcntAll.json",
                             {"crtfc_key": api_key, "corp_code": corp_code,
                              "bsns_year": str(year), "reprt_code": reprt_code,
                              "fs_div": fs_div}).json()
            parsed = parse_fnltt_single_acnt(resp)
            fs_payload["by_fs_div"][fs_div] = parsed
            fs_payload["status"] = parsed["status"]
            fs_payload["message"] = parsed["message"]
            fs_divs_collected.append(fs_div)
        except Exception as e:
            # 별도(OFS)가 없는 지주사도 있음 — 실패는 기록만 하고 진행.
            print(f"[collect_company] {company} fnltt {fs_div} 스킵 - {e}", file=sys.stderr)
    (d / "fs_structured.json").write_text(
        json.dumps(fs_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4) 재무제표 원본 XBRL → xbrl/ 해제 (rcept_no 필요)
    xbrl_files: List[str] = []
    if rcept_no:
        try:
            r = _http_get(client, f"{DART_BASE}/fnlttXbrl.xml",
                          {"crtfc_key": api_key, "rcept_no": rcept_no,
                           "reprt_code": reprt_code})
            xbrl_files = unzip_to(r.content, d / "xbrl")
        except Exception as e:
            print(f"[collect_company] {company} fnlttXbrl 스킵 - {e}", file=sys.stderr)

    # 5) 원문 문서 → source/ (표시용 PDF 는 보장 안 됨 — 받은 원문만 저장)
    source_files: List[str] = []
    if rcept_no:
        try:
            r = _http_get(client, f"{DART_BASE}/document.xml",
                          {"crtfc_key": api_key, "rcept_no": rcept_no})
            source_files = unzip_to(r.content, d / "source")
        except Exception as e:
            print(f"[collect_company] {company} document 스킵 - {e}", file=sys.stderr)

    # 6) 검토보고서(표시용 PDF) 첨부 수집 — OpenDartReader 2단계. 실패해도 None 반환(무회귀).
    review = fetch_review_attachment(odr, rcept_no, d) if (odr and rcept_no) else None

    # 7) 본문 보고서(표시용 PDF) 첨부 수집 — opt-in(collect_report). 무클로버.
    report_attach = (fetch_report_attachment(odr, rcept_no, d)
                     if (collect_report and odr and rcept_no) else None)
    report_pdf_ok = bool(report_attach and report_attach.get("display_ok"))

    # documents[]: 제출원문(report) + (성공 시)검토보고서(review). 기존 meta 키는 모두 유지(하위호환).
    report_doc = {
        "doc_type": "report",
        "source": "opendart document.xml",
        "rcept_no": rcept_no,
        "source_type": "full_report",
        "display_pdf": "report.pdf" if report_pdf_ok else "manual_upload_required",
    }
    if report_attach:
        report_doc["filename_dart"] = report_attach.get("filename_dart")
        report_doc["display_ok"] = report_attach.get("display_ok")
    documents: List[dict] = [report_doc]
    if review:
        documents.append(review)

    meta = {
        "company": company,
        "period": period,
        "corp_code": corp_code,
        "rcept_no": rcept_no,
        "report_nm": report_nm,
        "rcept_dt": rcept_dt,
        "reprt_code": reprt_code,
        "fs_divs": fs_divs_collected,
        "source_type": classify_source_type(report_nm or ""),
        "xbrl_files": xbrl_files,
        "source_files": source_files,
        "collected_at": now_iso(),
        # 표시용 PDF — collect_report 로 본문 PDF 수집 성공 시 report.pdf, 아니면 수동 업로드.
        "display_pdf": "report.pdf" if report_pdf_ok else "manual_upload_required",
        # 문서 묶음: report(제출원문) + review(검토보고서, 성공 시). Step B(main.py) 가 소비.
        "documents": documents,
        "review_collected": bool(review),
        "report_collected": report_pdf_ok,
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return meta


def run_live(companies: List[str], year: int, reprt_code: str,
             collect_report: bool = False) -> int:
    """라이브 수집 진입. 키 없으면 친절 안내 후 종료(에러 3요소).

    collect_report=True 면 본문 보고서 PDF(report.pdf)도 무클로버로 자동수집한다.
    """
    api_key = get_api_key()
    if not api_key:
        print("[run_live] DART_API_KEY 미설정 - .env 에 키가 없어 라이브 수집을 시작할 수 없습니다.",
              file=sys.stderr)
        print("  해결: backend\\.env 파일에 'DART_API_KEY=발급키' 1줄을 추가하세요.",
              file=sys.stderr)
        print("  키 없이 계획만 보려면 --dry-run 으로 실행하세요.", file=sys.stderr)
        return 2
    if not _HAS_HTTPX:
        print("[run_live] httpx 미설치 - 라이브 HTTP 호출 불가. 'pip install httpx' 후 재시도.",
              file=sys.stderr)
        return 3

    period = period_from_reprt(year, reprt_code)
    print(f"[run_live] 라이브 수집 시작 — key={mask_key(api_key)} period={period} "
          f"companies={companies}")

    # OpenDartReader 인스턴스 1회 생성(corpCode 일일캐시·docs_cache 재사용). 미설치 시 None.
    # 생성 실패도 키 노출 금지 → 타입명만. review 스킵, 기존 수집은 계속.
    odr = None
    if _HAS_OPENDART:
        try:
            odr = OpenDartReader(api_key)
        except Exception as e:
            print(f"[run_live] OpenDartReader 초기화 실패 ({type(e).__name__}) "
                  f"— 검토보고서 수집 스킵, 기존 수집은 계속.", file=sys.stderr)
            odr = None
    else:
        print("[run_live] OpenDartReader 미설치 - 검토보고서(review.pdf) 수집 스킵. "
              "보고서/XBRL/fnltt 수집은 계속. ('pip install \"OpenDartReader>=0.2,<0.3\"')",
              file=sys.stderr)

    with httpx.Client(timeout=60.0) as client:
        # 1) corp_code 매핑(전체 회사 공통, 1회)
        try:
            corp_codes = fetch_and_store_corp_codes(client, api_key)
        except Exception as e:
            print(f"[run_live] corpCode 수집 실패 - {e}", file=sys.stderr)
            corp_codes = resolve_corp_codes(_load_cached_corp_codes())

        for c in companies:
            corp_code = corp_codes.get(c)
            if not corp_code:
                print(f"[run_live] {c} corp_code 미확보 - 건너뜀(corpCode.xml 매칭 실패).",
                      file=sys.stderr)
                continue
            try:
                meta = collect_company(client, api_key, c, corp_code, year, reprt_code,
                                       period, odr=odr, collect_report=collect_report)
                review_note = ("review=OK(" + meta["documents"][-1]["filename_dart"] + ")"
                               if meta.get("review_collected") else "review=미수집")
                report_note = ("report=OK(" + (meta["documents"][0].get("filename_dart") or "") + ")"
                               if meta.get("report_collected")
                               else ("report=수집안함" if not collect_report else "report=미수집"))
                print(f"[run_live] {c} 완료 — rcept_no={meta['rcept_no']} "
                      f"fs_divs={meta['fs_divs']} xbrl={len(meta['xbrl_files'])}개 "
                      f"source={len(meta['source_files'])}개 {review_note} {report_note}")
            except Exception as e:
                print(f"[run_live] {c} 수집 실패 - {e}", file=sys.stderr)
    print("[run_live] 완료. 표시용 PDF 가 없으면 main.py /api/library/upload 로 수동 업로드하세요.")
    return 0


# ----------------------------------------------------------------------------
# 오프라인 셀프테스트 (라이브 키 불필요)
# ----------------------------------------------------------------------------
# 보유 XBRL zip(검증용). 없으면 라벨 추출 테스트는 스킵.
_SAMPLE_XBRL_ZIP = Path(r"C:\Users\2hryu\Downloads\[신한지주]분기보고서_IFRS(원문XBRL)(2026.05.15).zip")

# corpCode.xml 매칭 픽스처 (4사명 포함 소형 XML)
_CORP_CODE_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<result>
  <list><corp_code>00382199</corp_code><corp_name>신한지주</corp_name><stock_code>055550</stock_code></list>
  <list><corp_code>00688996</corp_code><corp_name>KB금융</corp_name><stock_code>105560</stock_code></list>
  <list><corp_code>00547583</corp_code><corp_name>하나금융지주</corp_name><stock_code>086790</stock_code></list>
  <list><corp_code>01511577</corp_code><corp_name>우리금융지주</corp_name><stock_code>316140</stock_code></list>
  <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list>
</result>""".encode("utf-8")

# fnlttSinglAcntAll.json 응답 픽스처(공식 스키마 축약)
_FNLTT_FIXTURE = {
    "status": "000",
    "message": "정상",
    "list": [
        {"rcept_no": "20250515000001", "bsns_year": "2025", "corp_code": "00382199",
         "sj_div": "BS", "sj_nm": "재무상태표", "fs_div": "CFS", "fs_nm": "연결재무제표",
         "account_id": "ifrs-full_CurrentAssets", "account_nm": "유동자산",
         "account_detail": "-", "thstrm_nm": "제24기", "thstrm_amount": "123456789",
         "frmtrm_nm": "제23기", "frmtrm_amount": "111111111", "ord": "1", "currency": "KRW"},
        {"rcept_no": "20250515000001", "bsns_year": "2025", "corp_code": "00382199",
         "sj_div": "IS", "sj_nm": "손익계산서", "fs_div": "CFS", "fs_nm": "연결재무제표",
         "account_id": "ifrs-full_Revenue", "account_nm": "영업수익",
         "account_detail": "-", "thstrm_nm": "제24기", "thstrm_amount": "987654321",
         "frmtrm_nm": "제23기", "frmtrm_amount": "888888888", "ord": "2", "currency": "KRW"},
    ],
}

_FNLTT_ERROR_FIXTURE = {"status": "013", "message": "조회된 데이타가 없습니다.", "list": []}


def run_self_test() -> int:
    """오프라인 검증: corpCode 매칭 / fnltt 파서 / (보유 시)XBRL 라벨 추출.

    라이브 키·HTTP 불필요. 실패 시 비0 종료코드.
    """
    fails = 0
    print("=" * 70)
    print("오프라인 셀프테스트 (라이브 키 불필요)")
    print("=" * 70)

    # 1) corpCode 매칭
    print("\n[1] corpCode.xml 4사 매칭")
    try:
        found = parse_corp_code_xml(_CORP_CODE_FIXTURE)
        resolved = resolve_corp_codes(found)
        expected = {"신한": "00382199", "KB": "00688996",
                    "하나": "00547583", "우리": "01511577"}
        for k, v in expected.items():
            ok = resolved.get(k) == v
            print(f"    {k:<3} → {resolved.get(k)}  {'OK' if ok else 'FAIL(기대=' + v + ')'}")
            if not ok:
                fails += 1
        # 삼성전자는 매칭 안 돼야 함(4사 외 제외 확인)
        if len(found) != 4:
            print(f"    FAIL: 4사만 매칭되어야 하는데 {len(found)}건"); fails += 1
        else:
            print(f"    OK: 4사 외(삼성전자) 제외 확인")
    except Exception as e:
        print(f"    FAIL: {e}"); fails += 1

    # 2) fnltt 파서 — 정상
    print("\n[2] fnlttSinglAcntAll 파서 (정상 응답)")
    try:
        parsed = parse_fnltt_single_acnt(_FNLTT_FIXTURE)
        ok = (parsed["account_count"] == 2 and "CFS" in parsed["fs_div_present"]
              and parsed["accounts"][0]["account_nm"] == "유동자산"
              and parsed["accounts"][0]["thstrm_amount"] == "123456789")
        print(f"    account_count={parsed['account_count']} "
              f"fs_div={parsed['fs_div_present']} sj_div={parsed['sj_div_present']}")
        print(f"    첫 계정: {parsed['accounts'][0]['account_nm']} = "
              f"{parsed['accounts'][0]['thstrm_amount']} (원문 보존, 환산 없음)")
        print(f"    {'OK' if ok else 'FAIL'}")
        if not ok:
            fails += 1
    except Exception as e:
        print(f"    FAIL: {e}"); fails += 1

    # 3) fnltt 파서 — 오류 status 는 예외
    print("\n[3] fnlttSinglAcntAll 파서 (오류 status → 예외)")
    try:
        parse_fnltt_single_acnt(_FNLTT_ERROR_FIXTURE)
        print("    FAIL: 오류 status 인데 예외가 안 났음"); fails += 1
    except ValueError:
        print("    OK: status≠000 에 ValueError")

    # 4) XBRL 라벨 추출 (보유 zip 있을 때만)
    print("\n[4] XBRL _lab-ko.xml 한국어 라벨 추출")
    if _SAMPLE_XBRL_ZIP.exists():
        try:
            lab = find_lab_ko_in_zip(_SAMPLE_XBRL_ZIP)
            if lab is None:
                print("    SKIP: zip 안에 _lab-ko.xml 없음")
            else:
                labels = extract_xbrl_labels(lab, limit=10)
                print(f"    추출 라벨 {len(labels)}개(앞 10개):")
                for lbl in labels:
                    print(f"      - {lbl}")
                if len(labels) >= 1:
                    print("    OK")
                else:
                    print("    FAIL: 라벨 0개"); fails += 1
        except Exception as e:
            print(f"    FAIL: {e}"); fails += 1
    else:
        print(f"    SKIP: 보유 zip 없음 - {_SAMPLE_XBRL_ZIP}")

    # 5) 검토보고서 후보 선택 — 연결검토보고서 > 검토보고서 우선순위
    print("\n[5] 검토보고서 후보 선택 (연결검토 > 검토 우선)")
    try:
        # (a) 연결검토보고서가 있으면 그것을 우선 선택(검토보고서/감사보고서보다 앞)
        docs_a = [
            {"title": "[신한지주]분기보고서(2025.11.14)", "url": "u_report"},
            {"title": "[신한지주]분기검토보고서(2025.11.14)", "url": "u_review"},
            {"title": "[신한지주]분기연결검토보고서(2025.11.14)", "url": "u_conn_review"},
        ]
        picked_a = pick_review_doc(docs_a)
        ok_a = picked_a is not None and picked_a["url"] == "u_conn_review"
        print(f"    (a) 연결검토 우선: 선택={picked_a['title'] if picked_a else None} "
              f"{'OK' if ok_a else 'FAIL'}")
        if not ok_a:
            fails += 1
        # (b) 연결검토 없으면 검토보고서 선택
        docs_b = [
            {"title": "[KB금융]분기보고서(2025.11)", "url": "u_report"},
            {"title": "[KB금융]검토보고서(2025.11)", "url": "u_review"},
        ]
        picked_b = pick_review_doc(docs_b)
        ok_b = picked_b is not None and picked_b["url"] == "u_review"
        print(f"    (b) 검토보고서 폴백: 선택={picked_b['title'] if picked_b else None} "
              f"{'OK' if ok_b else 'FAIL'}")
        if not ok_b:
            fails += 1
        # (c) 후보 없으면 None
        docs_c = [{"title": "[우리금융지주]분기보고서(2025.11)", "url": "u_report"}]
        picked_c = pick_review_doc(docs_c)
        ok_c = picked_c is None
        print(f"    (c) 후보 없음 → None: {'OK' if ok_c else 'FAIL'}")
        if not ok_c:
            fails += 1
    except Exception as e:
        print(f"    FAIL: {e}"); fails += 1

    # 5b) 본문 보고서 PDF 선택 — attach_files dict 에서 .pdf 만, XBRL zip 배제
    print("\n[5b] 본문 보고서 PDF 선택 (attach_files; XBRL zip 배제)")
    try:
        # 라이브 확인된 실제 형태: 본문 .pdf + XBRL 원문 .zip 공존
        files_r = {
            "[하나금융지주]분기보고서(2025.11.14).pdf": "u_pdf",
            "[하나금융지주]분기보고서_IFRS(원문XBRL)(2025.11.14).zip": "u_zip",
        }
        picked_r = pick_report_pdf(files_r)
        ok_r = picked_r is not None and picked_r[1] == "u_pdf"
        print(f"    (a) 본문 PDF 선택(zip 배제): 선택={picked_r[0] if picked_r else None} "
              f"{'OK' if ok_r else 'FAIL'}")
        if not ok_r:
            fails += 1
        # .pdf 없고 zip 만 있으면 None
        files_r2 = {"[KB금융]분기보고서_IFRS(원문XBRL)(2025.11).zip": "u_zip"}
        ok_r2 = pick_report_pdf(files_r2) is None
        print(f"    (b) zip 만 → None: {'OK' if ok_r2 else 'FAIL'}")
        if not ok_r2:
            fails += 1
    except Exception as e:
        print(f"    FAIL: {e}"); fails += 1

    # 6) 매직넘버 판정 — %PDF- vs 비PDF 바이트
    print("\n[6] 매직넘버 판정 (%PDF- vs 비PDF)")
    try:
        ok_pdf = is_pdf_bytes(b"%PDF-1.7\n...") is True
        ok_zip = is_pdf_bytes(b"PK\x03\x04") is False   # ZIP
        ok_html = is_pdf_bytes(b"<!DOCTYPE html>") is False
        ok_empty = is_pdf_bytes(b"") is False
        print(f"    %PDF-→PDF:{ok_pdf}  PK(zip)→비PDF:{ok_zip}  "
              f"html→비PDF:{ok_html}  빈바이트→비PDF:{ok_empty}")
        if all((ok_pdf, ok_zip, ok_html, ok_empty)):
            print("    OK")
        else:
            print("    FAIL"); fails += 1
    except Exception as e:
        print(f"    FAIL: {e}"); fails += 1

    print("\n" + "=" * 70)
    print(f"셀프테스트 결과: {'모두 통과' if fails == 0 else str(fails) + '건 실패'}")
    print("=" * 70)
    return 0 if fails == 0 else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collect_dart.py",
        description="DART OpenAPI 4대 금융지주 공시 자동수집 스캐폴드 (AI 미사용, 단위환산 없음)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  python collect_dart.py --companies 신한 KB 하나 우리 --year 2025 --reprt 11014 --dry-run\n"
            "  python collect_dart.py --companies 신한 --year 2025 --reprt 11011   # 라이브(키 필요)\n"
            "  python collect_dart.py --self-test                                  # 오프라인 검증\n\n"
            "reprt_code: 11013=Q1 11012=Q2(반기) 11014=Q3 11011=FY(사업)\n"
            "키: backend\\.env 의 DART_API_KEY (절대 인자로 넘기지 말 것)"
        ),
    )
    p.add_argument("--companies", nargs="+", default=["신한", "KB", "하나", "우리"],
                   choices=sorted(VALID_COMPANIES),
                   help="대상 회사(내부표기). 기본: 4사 전체")
    p.add_argument("--year", type=int, default=2025, help="사업연도(bsns_year). 기본 2025")
    p.add_argument("--reprt", default="11014", choices=sorted(REPRT_TO_PERIOD_SUFFIX),
                   help="보고서코드. 기본 11014(3분기)")
    p.add_argument("--dry-run", action="store_true",
                   help="키 없이 호출 계획/저장경로만 출력(라이브 호출 안 함)")
    p.add_argument("--self-test", action="store_true",
                   help="오프라인 파서 셀프테스트(키 불필요)")
    p.add_argument("--report-pdf", action="store_true",
                   help="본문 보고서 PDF(report.pdf) 도 자동수집(무클로버: 기존 report.pdf 보존). "
                        "OpenDartReader 필요. 하나/우리 등 '분기 –' 공백 셀 채우기용.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()
    if args.dry_run:
        run_dry_run(args.companies, args.year, args.reprt,
                    collect_report=args.report_pdf)
        return 0
    return run_live(args.companies, args.year, args.reprt,
                    collect_report=args.report_pdf)


if __name__ == "__main__":
    sys.exit(main())
