"""note_topics §5.2 표준 주제 매핑 검증 (결정론 임베딩 분류)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import note_topics as nt  # noqa: E402


def test_assign_above_and_below_threshold():
    topic_embs = {"공정가치": [1.0, 0.0, 0.0], "대손충당금": [0.0, 1.0, 0.0]}
    # 공정가치 토픽과 동일 방향 → 배정
    assert nt.assign_note_topic([1.0, 0.0, 0.0], topic_embs)["topic"] == "공정가치"
    # 어느 토픽과도 거의 직교 → 미분류(None)
    assert nt.assign_note_topic([0.0, 0.0, 1.0], topic_embs)["topic"] is None


def test_build_topic_map_alignment_and_unclassified():
    topic_embs = {"공정가치": [1.0, 0.0], "대손충당금": [0.0, 1.0]}
    company_notes = {
        "신한": [
            {"no": 1, "title": "공정가치측정", "page_start": 10, "embedding": [0.99, 0.01]},
            {"no": 2, "title": "충당금", "page_start": 20, "embedding": [0.02, 0.99]},
        ],
        "KB": [
            {"no": 5, "title": "공정가치 관련", "page_start": 30, "embedding": [0.97, 0.02]},
        ],
    }
    m = nt.build_topic_map(topic_embs, company_notes)
    assert m["matrix"]["공정가치"]["신한"][0]["note_no"] == 1
    assert m["matrix"]["대손충당금"]["신한"][0]["note_no"] == 2
    assert m["matrix"]["공정가치"]["KB"][0]["note_no"] == 5
    assert m["unclassified"]["신한"] == 0
