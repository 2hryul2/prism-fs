"""주석 RAG(§5.4) 인용 강제 게이트 — AI 답변은 반드시 출처(sources) 동반."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import notes_rag  # noqa: E402


def test_build_prompt_injects_citation_labels():
    srcs = [
        {"company": "신한", "period": "2026Q1", "fs_div": "연결", "note_no": 8,
         "page_start": 175, "text": "공정가치 측정 관련 서술..."},
        {"company": "KB", "period": "2026Q1", "fs_div": "연결", "note_no": 9,
         "page_start": 60, "text": "대손충당금 정책..."},
    ]
    p = notes_rag.build_prompt("질문", srcs)
    assert "[출처: 신한 2026Q1 연결 주석8 p175]" in p
    assert "[출처: KB 2026Q1 연결 주석9 p60]" in p
    # 숫자 생성 금지 지시 포함
    assert "계산하거나 생성하지" in p


def test_retrieve_filters_fs_div_and_sorts():
    q = [1.0, 0.0, 0.0]
    cells = [{"company": "신한", "period": "2026Q1", "index": {"notes": [
        {"no": 1, "title": "연결주석", "fs_div": "연결", "page_start": 10, "page_end": 11, "embedding": [1.0, 0.0, 0.0]},
        {"no": 2, "title": "별도주석", "fs_div": "별도", "page_start": 20, "page_end": 21, "embedding": [1.0, 0.0, 0.0]},
        {"no": 3, "title": "약한연결", "fs_div": "연결", "page_start": 30, "page_end": 31, "embedding": [0.2, 0.9, 0.0]},
    ]}}]
    res = notes_rag.retrieve(q, cells, fs_div="연결", top_k=5)
    assert all(r["fs_div"] == "연결" for r in res)           # 별도 제외
    assert [r["note_no"] for r in res] == [1, 3]             # 점수 내림차순
    assert res[0]["score"] >= res[1]["score"]


def test_retrieve_empty_when_no_matching_fsdiv():
    # 별도 요청인데 별도 노트 없음 → 빈 결과 → (엔드포인트에서) answer 생성 금지
    cells = [{"company": "신한", "period": "2026Q1", "index": {"notes": [
        {"no": 1, "title": "연결만", "fs_div": "연결", "page_start": 1, "page_end": 1, "embedding": [1.0, 0.0]},
    ]}}]
    res = notes_rag.retrieve([1.0, 0.0], cells, fs_div="별도", top_k=5)
    assert res == []


def test_answer_returns_none_without_sources():
    # 근거 없으면 생성 금지(인용 강제). httpx 호출 전 단락.
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        notes_rag.answer_ollama("질문", [], "http://localhost:11434", "qwen2.5:7b-instruct"))
    assert out is None


# --- 라이브 엔드포인트 계약(서버 가동 시): answer 있으면 sources 비어있지 않음 ---
def test_endpoint_answer_requires_sources_live():
    try:
        import httpx
        r = httpx.get("http://localhost:8021/api/notes/rag",
                      params={"q": "공정가치 측정 수준", "fs_div": "연결"}, timeout=120)
    except Exception:
        pytest.skip("prism-fs 서버(:8021) 미가동")
    assert r.status_code == 200
    d = r.json()
    assert "sources" in d                       # 항상 sources 키 존재
    if d.get("answer"):
        assert len(d["sources"]) > 0            # 인용 없는 생성 금지


# ---- §5.3 비교 메모 초안 인용 강제 ----
def test_compare_prompt_injects_citations_and_no_number_rule():
    srcs = [{"company": "신한", "period": "2026Q1", "fs_div": "연결", "note_no": 8, "page_start": 175, "text": "공정가치 정책..."},
            {"company": "KB", "period": "2026Q1", "fs_div": "연결", "note_no": 22, "page_start": 287, "text": "공정가치 측정..."}]
    p = notes_rag.build_compare_prompt("공정가치", srcs)
    assert "[출처: 신한 2026Q1 연결 주석8 p175]" in p
    assert "[출처: KB 2026Q1 연결 주석22 p287]" in p
    assert "생성하지" in p  # 숫자 생성 금지 지시


def test_compare_memo_none_without_sources():
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        notes_rag.answer_compare_ollama("공정가치", [], "http://localhost:11434", "qwen2.5:7b-instruct"))
    assert out is None


def test_compare_memo_endpoint_contract_live():
    try:
        import httpx
        r = httpx.get("http://localhost:8021/api/notes/compare-memo",
                      params={"topic": "공정가치", "period": "2026Q1", "fs_div": "연결"}, timeout=300)
    except Exception:
        pytest.skip("서버 미가동")
    assert r.status_code == 200
    d = r.json()
    assert "sources" in d
    if d.get("memo"):
        assert len(d["sources"]) > 0  # 인용 없는 초안 금지


# ---- P7: OpenAI 옵트인 — provider 미설정 시 안전 반환 ----
def test_generate_returns_none_when_no_provider():
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        notes_rag._generate("프롬프트", openai_key=None, ollama_url="", ollama_model=""))
    assert out is None
