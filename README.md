# prism-fs

**4대 금융지주 주석·재무제표 비교 대시보드** — 신한금융지주 회계팀 PoC

> 버전 0.1.0 · Windows 11 / 폐쇄망 친화 · 독립 신규 프로젝트 (`prism` 자매 프로젝트)

## 목적
년도·분기별 4대 금융지주(신한·KB·하나·우리)의 **연결(CFS)·별도(OFS)** 재무제표와 주석을
한 화면에서 검색·비교하고, 회계담당자의 재무제표 비교분석 업무를 돕는다.

1. 주석·주기(회계정책)·서술형 주석 **검색·비교조회**
2. 라이브러리 매트릭스에 **연결·별도 보고서 둘 다** 수집 (연결/별도가 1급 차원)
3. 회계담당자용 **비교분석 기능** (시계열 증감·연결vs별도 차이·4사 벤치마킹·구조비율 등)

## 절대 안전경계 (불변)
- **AI 숫자 재구성 금지** — DART/XBRL 원문 그대로
- **단위 환산 금지** — 보조 표기는 별도 라인만
- **파생값은 결정론 계산만** — 결과마다 계산식·입력 원문(provenance) 동봉, LLM 무경유
- 외부 LLM은 `.env` 키 옵트인(기본 로컬 Ollama) · 키 마스킹(DART/OPENAI/sk-*/Bearer)
- **폐쇄망**: 외부 CDN 0 (순수 CSS + 인라인 JS, vendored 자산)

## 현재 상태 (v0.1.0 — 기획 + UI 프로토타입)
- ✅ 아이데이션 문서: `ideation/`
- ✅ EARS 스펙: `spec_20260601_0937.md`
- ✅ UI 프로토타입(오프라인 클릭 가능): `src/static/index.html`
  - 실제 `fs_structured.json` 슬라이스 임베드(4사×2기간×연결/별도, 원문 그대로)
- ⏳ 백엔드(FastAPI) — 다음 세션 (`prism` 의 collect_dart/compare/coverage 이식)

## 프로토타입 실행
```powershell
# 백엔드 없이 단일 HTML — 브라우저로 직접 열기
start src\static\index.html
```

## 폴더 구조
```
prism-fs\
├── ideation\          아이데이션 문서
├── doc\               개발노트
├── spec_*.md          EARS 스펙
├── src\static\        UI 프로토타입 (단일 HTML)
├── VERSION            0.1.0
├── .env.example       시크릿 키 이름만
├── requirements.txt   백엔드 의존성(다음 세션)
└── dist\setup\        인스톨러 (추후)
```

## 스택
| 레이어 | 선택 | 근거 |
|---|---|---|
| 백엔드 | Python 3.11 + FastAPI | DART/임베딩 생태계, prism 직접 이식 |
| 프론트 | Vanilla JS 단일 HTML + 순수 CSS | 빌드툴 0, 폐쇄망 친화 |
