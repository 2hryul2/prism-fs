"""주석 XBRL 상세태깅 진단 사전계산 CLI (개발 PC 전용).

storage 에 보존된 XBRL(instance·xsd·linkbase)을 파싱해 셀별 진단 결과를
`storage/library/<회사>/<기간>/xbrl_tagging.json` 으로 기록한다. 런타임 엔드포인트·UI 는
이 JSON 만 읽으므로(대용량 파싱은 여기서 1회) 요청 경로에 파싱 부하가 없다.

네트워크·외부 API(DART/LLM) 호출 절대 없음. 숫자 AI 무경유(순수 결정론 집계).
exe 번들 제외 — prism_fs.spec datas/hiddenimports 에 추가하지 말 것.

사용 예:
  python scripts\\build_xbrl_tagging.py build 신한 2026Q1
  python scripts\\build_xbrl_tagging.py build-all
  python scripts\\build_xbrl_tagging.py matrix            # 콘솔 요약
  python scripts\\build_xbrl_tagging.py matrix --csv out.csv
"""
import sys
import json
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"


def _import_engine():
    """src 를 sys.path 에 추가 후 xbrl_tagging import(경로 헬퍼·파싱 재사용)."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import xbrl_tagging
    return xbrl_tagging


def _discover_cells(X) -> list:
    """LIBRARY_ROOT 아래 xbrl/ 가 있는 (회사,기간) 셀 전부 발견(정렬)."""
    root = Path(X.LIBRARY_ROOT)
    cells = []
    if not root.exists():
        return cells
    for co in sorted(p.name for p in root.iterdir() if p.is_dir()):
        for pd in sorted(p.name for p in (root / co).iterdir() if p.is_dir()):
            if (root / co / pd / "xbrl").exists():
                cells.append((co, pd))
    return cells


def _out_path(X, company: str, period: str) -> Path:
    return Path(X.LIBRARY_ROOT) / company / period / "xbrl_tagging.json"


def _write_diag(X, company: str, period: str) -> dict:
    """1셀 진단 → xbrl_tagging.json 기록. 진단 dict 반환."""
    diag = X.diagnose_one(company, period)
    out = _out_path(X, company, period)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=2)
    return diag


def _summary_line(company: str, period: str, diag: dict) -> str:
    if diag.get("status") != "ok":
        return f"[{company} {period}] {diag.get('status')}"
    t = diag["l2"]["totals"]
    l1 = diag["l1"]
    l3 = diag["l3"]["overall"]
    return (f"[{company} {period}] facts={t['facts']} (num={t['numeric']} tb={t['textblock']}) "
            f"주석 used/decl={l1['notes_used']}/{l1['notes_declared']}({l1['used_ratio']}) "
            f"L3={l3['matched']}/{l3['total']}({l3['rate']})")


def cmd_build(args) -> int:
    X = _import_engine()
    diag = _write_diag(X, args.company, args.period)
    print(_summary_line(args.company, args.period, diag))
    print(f"기록: {_out_path(X, args.company, args.period)}")
    return 0 if diag.get("status") == "ok" else 1


def cmd_build_all(args) -> int:
    X = _import_engine()
    cells = _discover_cells(X)
    if not cells:
        print("셀 없음 — LIBRARY_ROOT 확인")
        return 1
    ok = 0
    for co, pd in cells:
        diag = _write_diag(X, co, pd)
        print(_summary_line(co, pd, diag))
        ok += 1 if diag.get("status") == "ok" else 0
    print(f"=== 완료: {ok}/{len(cells)} 셀 진단 기록 ===")
    return 0


def cmd_matrix(args) -> int:
    """4사×분기 횡단 요약 — 기간편차(used_ratio·fact깊이·textblock·L3) 한눈에."""
    X = _import_engine()
    cells = _discover_cells(X)
    rows = []
    for co, pd in cells:
        diag = X.diagnose_one(co, pd)
        if diag.get("status") != "ok":
            rows.append([co, pd, "", "", "", "", "", diag.get("status", "?")])
            continue
        t = diag["l2"]["totals"]
        l1 = diag["l1"]
        l3 = diag["l3"]["overall"]
        rows.append([co, pd, t["facts"], t["textblock"],
                     l1["notes_used"], l1["notes_declared"], l1["used_ratio"],
                     f"{l3['matched']}/{l3['total']}={l3['rate']}"])

    header = ["회사", "기간", "facts", "textblock", "주석used", "주석decl", "used_ratio", "L3매칭"]
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"CSV 기록: {args.csv} ({len(rows)}행)")
        return 0
    # 콘솔 표
    print("  ".join(f"{h:>9}" if i >= 2 else f"{h:<7}" for i, h in enumerate(header)))
    for r in rows:
        cells_str = [f"{r[0]:<7}", f"{r[1]:<7}"] + [f"{str(c):>9}" for c in r[2:]]
        print("  ".join(cells_str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_xbrl_tagging",
        description="주석 XBRL 상세태깅 진단 사전계산 (개발 PC 전용, 오프라인).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("build", help="1셀 진단 → xbrl_tagging.json")
    pb.add_argument("company")
    pb.add_argument("period")
    pb.set_defaults(func=cmd_build)

    pa = sub.add_parser("build-all", help="발견된 전 셀 진단 기록")
    pa.set_defaults(func=cmd_build_all)

    pm = sub.add_parser("matrix", help="횡단 요약(콘솔 또는 --csv)")
    pm.add_argument("--csv", help="CSV 출력 경로")
    pm.set_defaults(func=cmd_matrix)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
