"""note_filters 주기/서술형 결정론 분류 검증."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import note_filters as nf  # noqa: E402


def test_note_kind_policy_vs_narrative():
    assert nf.note_kind("중요한 회계정책") == "주기"
    assert nf.note_kind("재무제표 작성기준") == "주기"
    assert nf.note_kind("일반사항") == "주기"
    assert nf.note_kind("금융위험관리") == "서술형"
    assert nf.note_kind("우발 및 약정사항") == "서술형"
    assert nf.note_kind("당기손익-공정가치측정금융자산") == "서술형"


def test_matches_kind_all_passes():
    assert nf.matches_kind("아무 제목", "전체")
    assert nf.matches_kind("아무 제목", "")
    assert nf.matches_kind("중요한 회계정책", "주기")
    assert not nf.matches_kind("금융위험관리", "주기")
    assert nf.matches_kind("금융위험관리", "서술형")


def test_filter_notes():
    notes = [{"title": "중요한 회계정책"}, {"title": "금융위험관리"}, {"title": "일반사항"}]
    assert len(nf.filter_notes(notes, "전체")) == 3
    assert {n["title"] for n in nf.filter_notes(notes, "주기")} == {"중요한 회계정책", "일반사항"}
    assert {n["title"] for n in nf.filter_notes(notes, "서술형")} == {"금융위험관리"}
