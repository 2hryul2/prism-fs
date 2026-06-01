"""
note_topics.py — §5.2 표준 주제 매핑 · prism-fs

회사마다 번호·제목이 다른 주석을 **canonical topic**(통제어휘)으로 분류·정렬.
임베딩 cosine 결정론(AI 생성 무경유). 임계 미만은 "미분류".
숫자 무관(주석 제목 텍스트만). 호출부(app.py)가 topic/note 임베딩을 제공.
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

TOPIC_MIN_SCORE = 0.45  # 이 미만이면 미분류(coverage MIN_MATCH_SCORE 와 동일 기조)


def _cos(a, b) -> float:
    a, b = np.array(a), np.array(b)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def assign_note_topic(note_emb, topic_embs: Dict[str, List[float]]) -> Dict[str, Any]:
    """note 임베딩 → 최근접 canonical topic(+score). 임계 미만이면 topic=None(미분류)."""
    best_topic, best_score = None, -1.0
    for t, te in topic_embs.items():
        s = _cos(note_emb, te)
        if s > best_score:
            best_topic, best_score = t, s
    if best_score < TOPIC_MIN_SCORE:
        return {"topic": None, "score": round(best_score, 4)}
    return {"topic": best_topic, "score": round(best_score, 4)}


def build_topic_map(topic_embs: Dict[str, List[float]],
                    company_notes: Dict[str, List[dict]]) -> Dict[str, Any]:
    """topic × 회사 정렬표 + 회사별 미분류 수.

    company_notes: {회사: [note(embedding 포함)...]}
    반환: {
      "topics": [...],
      "matrix": {topic: {회사: [ {note_no,title,page_start,page_end,score} ... 상위순 ]}},
      "unclassified": {회사: 미분류 note 수},
    }
    """
    topics = list(topic_embs.keys())
    matrix: Dict[str, Dict[str, List[dict]]] = {t: {} for t in topics}
    unclassified: Dict[str, int] = {}
    for company, notes in company_notes.items():
        unc = 0
        per_topic: Dict[str, List[dict]] = {t: [] for t in topics}
        for n in notes:
            emb = n.get("embedding")
            if not emb:
                continue
            a = assign_note_topic(emb, topic_embs)
            if a["topic"] is None:
                unc += 1
                continue
            per_topic[a["topic"]].append({
                "note_no": n.get("no"), "title": n.get("title"),
                "page_start": n.get("page_start"), "page_end": n.get("page_end"),
                "score": a["score"],
            })
        for t in topics:
            per_topic[t].sort(key=lambda x: x["score"], reverse=True)
            matrix[t][company] = per_topic[t]
        unclassified[company] = unc
    return {"topics": topics, "matrix": matrix, "unclassified": unclassified}
