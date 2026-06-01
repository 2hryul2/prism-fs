"""
safety.py — 안전경계 헬퍼 중앙화 (prism-fs)

두 가지 횡단 관심사를 한 곳에 모은다:
1. 시크릿 마스킹: 예외/로그 노출 전 DART(crtfc_key)·OpenAI(OPENAI_API_KEY/sk-*)·Bearer 토큰 제거.
2. provenance: 결정론 파생값에 동봉하는 표준 근거 딕셔너리(계산식·입력 원문·결과·engine).

fs_compare(정량 결정론)와 app(_safe_err)이 공용으로 사용한다. AI/LLM 무경유.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# 마스킹 규칙(순서 무관). 각 패턴은 키 값만 ****로 치환하고 키 이름은 보존한다.
_MASK_RULES = (
    (re.compile(r"(crtfc_key=)[^&\s]+"), r"\1****"),               # DART OpenAPI 키
    (re.compile(r"(OPENAI_API_KEY[=:]\s*)\S+"), r"\1****"),        # 환경변수 형태
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "sk-****"),           # OpenAI 키 표준 접두
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1****"),
)


def mask_secrets(text: str) -> str:
    """문자열 내 시크릿(DART/OpenAI/Bearer)을 마스킹. 키 이름은 남기고 값만 ****."""
    out = text or ""
    for pat, repl in _MASK_RULES:
        out = pat.sub(repl, out)
    return out


def safe_err(e: Exception) -> str:
    """예외 → 사용자 노출용 메시지. 시크릿 마스킹 후 300자 상한(내부 구조 과다 노출 방지)."""
    return mask_secrets(f"{type(e).__name__}: {e}")[:300]


def provenance(formula: str, inputs: List[Dict[str, Any]], result: str,
               engine: str = "deterministic") -> Dict[str, Any]:
    """결정론 파생값 근거 딕셔너리 생성(표준 형태).

    Args:
        formula: 계산식 텍스트(예: "Δ = 당기 − 전기말").
        inputs: 입력 원문 리스트 — 각 항목 {label, raw, account_id, period, fs_div}.
        result: 결과 표기 문자열.
        engine: 계산 주체. 항상 "deterministic"(AI 무경유 보증 표식).
    """
    return {"formula": formula, "inputs": inputs, "result": result, "engine": engine}
