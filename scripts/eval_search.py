"""주석 검색 골든셋 평가 루프 (개발 PC 전용).

골든 질의셋(golden_search.json)을 가동 중인 prism-fs 서버(/api/notes/rag)로 질의해
hit@1/3/5 · MRR 을 카테고리별·전체로 집계한다. 가중·임계·동의어 튜닝의 근거 측정용.

표준 라이브러리만(urllib). 서버는 별도 가동 필요(기본 http://127.0.0.1:8021).
네트워크는 로컬 서버만 — 외부 호출 없음.

사용 예:
  python scripts\\eval_search.py
  python scripts\\eval_search.py --base http://127.0.0.1:8021 --k 5 --json out.json
"""
import re
import sys
import json
import argparse
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_search.json"


def _fetch(base: str, params: dict) -> dict:
    """GET /api/notes/rag — 로컬 서버 질의(검색만, LLM 생성 생략). 실패 시 예외."""
    params = {**params, "generate": "false"}  # 평가는 sources 만 필요 → 고속
    url = f"{base}/api/notes/rag?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _rank_of(sources: list, title_any: list) -> int:
    """정답(title 에 title_any 부분문자열 포함) 의 1-based 최초 순위. 없으면 0."""
    for i, s in enumerate(sources, start=1):
        title = s.get("title") or ""
        if any(t in title for t in title_any):
            return i
    return 0


def run(base: str, k: int) -> dict:
    spec = json.loads(GOLDEN.read_text(encoding="utf-8"))
    d = spec.get("defaults", {})
    rows = []
    for item in spec["queries"]:
        params = {
            "q": item["q"],
            "fs_div": item.get("fs_div", d.get("fs_div", "연결")),
            "companies": item.get("companies", d.get("companies", "")),
            "period": item.get("period", d.get("period", "")),
            "top_k": k,
        }
        try:
            resp = _fetch(base, params)
            sources = resp.get("sources", [])[:k]
            rank = _rank_of(sources, item["title_any"])
        except Exception as e:
            rows.append({**item, "rank": -1, "error": str(e)[:80]})
            continue
        rows.append({"q": item["q"], "cat": item.get("cat", "-"), "rank": rank,
                     "top": (sources[0]["title"][:22] if sources else "")})
    return _aggregate(rows)


def _aggregate(rows: list) -> dict:
    def _metrics(rs):
        n = len(rs) or 1
        h1 = sum(1 for r in rs if 1 <= r["rank"] <= 1) / n
        h3 = sum(1 for r in rs if 1 <= r["rank"] <= 3) / n
        h5 = sum(1 for r in rs if 1 <= r["rank"] <= 5) / n
        mrr = sum((1.0 / r["rank"]) for r in rs if r["rank"] >= 1) / n
        return {"n": len(rs), "hit@1": round(h1, 3), "hit@3": round(h3, 3),
                "hit@5": round(h5, 3), "mrr": round(mrr, 3)}

    cats = {}
    for r in rows:
        cats.setdefault(r.get("cat", "-"), []).append(r)
    return {
        "overall": _metrics(rows),
        "by_cat": {c: _metrics(rs) for c, rs in sorted(cats.items())},
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 동의어 프로브 — synonyms.py 그룹에서 교차변형 테스트를 자동 생성(평가셋 반영도 측정).
#   각 그룹의 한 변형으로 질의 → 코퍼스(제목)에 실재하는 다른 변형 표기 노트가 top-k 에 잡히는가.
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """정규화 — 공백·하이픈·중점·괄호 제거(약어 텍스트형 '기타포괄손익공정가치측정' ↔
    제목 '기타포괄손익-공정가치측정' 매칭, 본문 표기차 흡수)."""
    return re.sub(r"[\s\-ㆍ·()（）]+", "", s or "")


def _repo_corpus(period: str = "2026Q1") -> tuple:
    """src/storage 의 셀별 index.json → (연결 제목 리스트, 정규화 본문 블롭).

    본문 블롭 = 제목 + 청크 텍스트 정규화 결합 → 본문 기반 프로브(제목 미등장 그룹 커버)."""
    root = HERE.parent / "src" / "storage" / "library"
    titles, blob = [], []
    for idx in root.glob(f"*/{period}/index.json"):
        try:
            d = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in d.get("notes", []):
            if n.get("fs_div") != "연결":
                continue
            titles.append(n.get("title", ""))
            blob.append(_norm(n.get("title", "")))
            for ch in n.get("chunks", []):
                blob.append(_norm(ch.get("text", "")))
    return titles, "".join(blob)


def run_probe(base: str, k: int, period: str = "2026Q1") -> dict:
    """synonyms 그룹→프로브 자동생성·실행. 동의어 커버리지 + 교차변형 hit@k 집계.

    in_corpus 판정·hit 판정 모두 정규화 매칭(제목+본문) — 하이픈/약어 텍스트형도 테스트.
    """
    sys.path.insert(0, str(HERE.parent / "src"))
    import synonyms
    titles, corpus_blob = _repo_corpus(period)
    groups = synonyms._SYNONYM_GROUPS
    rows, testable = [], 0
    for g in groups:
        # 코퍼스(제목+본문, 정규화)에 실재하는 표기 — 정답 표기 후보
        in_corpus = sorted(t for t in g if _norm(t) in corpus_blob)
        if not in_corpus or len(g) < 2:
            rows.append({"group": sorted(g), "testable": False, "in_corpus": in_corpus})
            continue
        testable += 1
        in_norm = [_norm(t) for t in in_corpus]
        probes = []
        for qt in sorted(g):
            try:
                resp = _fetch(base, {"q": qt, "fs_div": "연결", "companies": "",
                                     "period": period, "top_k": k})
                src = resp.get("sources", [])[:k]
                rank = 0
                for i, s in enumerate(src, 1):
                    hay = _norm((s.get("title") or "") + (s.get("snippet") or ""))
                    if any(ic in hay for ic in in_norm):
                        rank = i
                        break
            except Exception:
                rank = -1
            probes.append({"q": qt, "rank": rank})
        hit = sum(1 for p in probes if p["rank"] >= 1)
        rows.append({"group": sorted(g), "testable": True, "in_corpus": in_corpus,
                     "probes": probes, "hit": hit, "n": len(probes)})
    tested = [r for r in rows if r.get("testable")]
    tot_hit = sum(r["hit"] for r in tested)
    tot_n = sum(r["n"] for r in tested) or 1
    return {
        "coverage": {"groups": len(groups), "testable": testable,
                     "coverage_ratio": round(testable / max(len(groups), 1), 3)},
        "cross_variant_hit": round(tot_hit / tot_n, 3),
        "rows": rows,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="주석 검색 골든셋·동의어 프로브 평가(hit@k·MRR).")
    p.add_argument("--base", default="http://127.0.0.1:8021", help="서버 주소")
    p.add_argument("--k", type=int, default=5, help="top_k")
    p.add_argument("--json", help="결과 JSON 저장 경로")
    p.add_argument("--probe", action="store_true", help="동의어 프로브 모드(사전 교차변형 자동테스트)")
    args = p.parse_args(argv)

    if args.probe:
        pr = run_probe(args.base, args.k)
        c = pr["coverage"]
        print(f"=== 동의어 평가셋 반영도 ===")
        print(f"  사전 그룹 {c['groups']}개 중 테스트가능(코퍼스 ≥1 표기·≥2 변형) {c['testable']}개 "
              f"→ 커버리지 {c['coverage_ratio']}")
        print(f"  교차변형 hit@{args.k} (전체 프로브): {pr['cross_variant_hit']}")
        print("=== 그룹별 ===")
        for r in pr["rows"]:
            if not r["testable"]:
                print(f"  [미테스트] {r['group']}  (코퍼스 표기 {r['in_corpus'] or '없음'})")
                continue
            miss = [pp["q"] for pp in r["probes"] if pp["rank"] < 1]
            flag = "" if not miss else f"  미적중: {miss}"
            print(f"  [{r['hit']}/{r['n']}] {r['group']}  정답표기={r['in_corpus']}{flag}")
        if args.json:
            Path(args.json).write_text(json.dumps(pr, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"기록: {args.json}")
        return 0

    res = run(args.base, args.k)
    print(f"=== 전체 (n={res['overall']['n']}) ===")
    o = res["overall"]
    print(f"  hit@1={o['hit@1']}  hit@3={o['hit@3']}  hit@5={o['hit@5']}  MRR={o['mrr']}")
    print("=== 카테고리별 ===")
    for c, m in res["by_cat"].items():
        print(f"  {c:6} n={m['n']:2}  hit@1={m['hit@1']:<5} hit@3={m['hit@3']:<5} "
              f"hit@5={m['hit@5']:<5} MRR={m['mrr']}")
    print("=== 케이스별 순위(rank=0 미발견, -1 오류) ===")
    for r in res["rows"]:
        flag = "" if r["rank"] >= 1 else "  <<<"
        print(f"  [{r.get('cat','-'):6}] rank={r['rank']:>2}  {r['q'][:28]:30} → {r.get('top','')}{flag}")

    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"기록: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
