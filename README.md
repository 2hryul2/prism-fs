# prism-fs

**4대 금융지주 주석·재무제표 비교 대시보드** — 신한금융지주 회계팀 PoC

> 버전 0.1.0 · Windows 11 / 폐쇄망 친화 · 독립 신규 프로젝트 (`prism` 자매 프로젝트)
> 백엔드(FastAPI) + 단일 HTML UI 구현 완료 · pytest 63 통과 · PyInstaller onedir 배포 가능

## 목적
년도·분기별 4대 금융지주(신한·KB·하나·우리)의 **연결(CFS)·별도(OFS)** 재무제표와 주석을
한 화면에서 검색·비교하고, 회계담당자의 재무제표 비교분석 업무를 돕는다.

1. 주석·주기(회계정책)·서술형 주석 **검색·비교조회** (임베딩+BM25 하이브리드)
2. 라이브러리 매트릭스에 **연결·별도 보고서 둘 다** 수집 (연결/별도가 1급 차원)
3. 회계담당자용 **비교분석 기능** (시계열 증감·연결vs별도 차이·4사 벤치마킹·구조비율 등)
4. 주석 **XBRL 상세태깅 진단** (DART 원본 XBRL 기준 태깅 범위·충실도 정량화)

## 절대 안전경계 (불변)
- **AI 숫자 재구성 금지** — DART/XBRL 원문 그대로
- **단위 환산 금지** — 보조 표기는 별도 라인만
- **파생값은 결정론 계산만** — 결과마다 계산식·입력 원문(provenance) 동봉, LLM 무경유
- 외부 LLM은 `.env` 키 옵트인(기본 로컬 Ollama) · 키 마스킹(DART/OPENAI/sk-*/Bearer)
- **폐쇄망**: 외부 CDN 0 (순수 CSS + 인라인 JS, vendored 자산 / `HF_HUB_OFFLINE`)

## 현재 상태 (v0.1.0)
- ✅ 아이데이션 문서: `ideation/`
- ✅ EARS 스펙: `spec_20260601_0937.md`
- ✅ **백엔드 (FastAPI, 32개 엔드포인트)**: `src/app.py` 외 10개 모듈
  - DART 자동수집 · 라이브러리 인덱싱 · 재무제표 결정론 비교(provenance)
  - 주석 RAG 검색(임베딩+BM25+동의어, 인용 강제) · 주제 매핑 · 비교메모(LLM 옵트인)
  - XBRL 상세태깅 진단(L1 범위·L2 차원인지 충실도·L3 PDF대조)
- ✅ **단일 HTML UI**: `src/static/index.html` (오프라인·외부 CDN 0, vendored pdf.js)
- 🧪 **Graph-RAG 온톨로지 데모**(차기 워크스트림): `src/static/concept_graph_demo.html` — "설명 가능한 검색" 시연. 기획: [ideation/ideation_주석검색_GraphRAG온톨로지_20260606.md](ideation/ideation_주석검색_GraphRAG온톨로지_20260606.md)
- ✅ **테스트**: `tests/` — 단위 + Playwright E2E, **pytest 63 통과**
- ✅ **배포**: PyInstaller onedir 풀번들 (`prism_fs.spec` + `build.ps1` + `run_server.py`)
- ✅ **오프라인 큐레이션 도구**(개발 PC 전용): `scripts/` — 인덱스 큐레이션·XBRL 태깅·검색 평가

> 데이터(`storage/`)·빌드 산출물(`dist/`)·임베딩 모델(`src/models/`)은 gitignore — 코드/문서만 추적.

## 실행

### 백엔드 서버 (개발)
```powershell
pip install -r requirements.txt
python src\run_server.py          # uvicorn :8021 기동 + 브라우저 자동 오픈
# 또는: uvicorn app:app --reload --host 127.0.0.1 --port 8021   (작업 디렉토리 src\)
```
- (권장) 로컬 한국어 임베딩 모델 사전 다운로드 — 부재 시 bigram 폴백(정확도 저하 배너):
  ```powershell
  python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('jhgan/ko-sroberta-multitask').save('./models/ko-sroberta')"
  $env:EMBED_MODEL_PATH = ".\models\ko-sroberta"
  ```
- `.env` (옵트인): `DART_API_KEY`(DART 수집), `OPENAI_API_KEY`(LLM 보조, 미설정 시 결정론만)

### UI 프로토타입만 (백엔드 없이)
```powershell
start src\static\index.html       # 임베드된 fs_structured.json 슬라이스로 동작
```

### 배포 빌드 (개발 PC 전용)
```powershell
.\build.ps1                       # PyInstaller onedir → dist\setup\ (torch+ST+모델 동봉)
```

### 테스트
```powershell
python -m pytest tests -q         # 단위 + E2E (서버 미가동 시 E2E graceful skip)
```

## 폴더 구조
```
prism-fs\
├── src\
│   ├── app.py                FastAPI 메인 (32개 엔드포인트)
│   ├── run_server.py         데스크톱 진입점 (uvicorn :8021 + 브라우저 자동오픈)
│   ├── paths.py              경로 중앙화 (dev/PyInstaller frozen 일관)
│   ├── collect_dart.py       DART OpenAPI 자동수집
│   ├── fs_compare.py         재무제표 결정론 비교 엔진 (provenance)
│   ├── notes_rag.py          주석 RAG (정성 텍스트 전용, 숫자 무경유)
│   ├── note_filters.py       주석 종류 분류 (주기/서술형)
│   ├── note_topics.py        §5.2 표준 주제 매핑
│   ├── safety.py             시크릿 마스킹 + provenance 중앙화
│   ├── synonyms.py           회계 동의어 쿼리 확장 (BM25/lexical, 결정론)
│   ├── synonyms_data.json    동의어 사전 데이터 (비개발자 편집용)
│   ├── xbrl_tagging.py       XBRL 상세태깅 진단 엔진 (L1/L2/L3)
│   └── static\               단일 HTML UI + vendored pdf.js
├── tests\                    단위 테스트 + e2e\ (Playwright)
├── scripts\                  개발 PC 전용 오프라인 도구 (exe 번들 제외)
│   ├── curate_index_claude.py   Claude 인덱스 큐레이션 (validate/diff/apply)
│   ├── build_xbrl_tagging.py    XBRL 태깅 사전계산 (build/build-all/matrix)
│   ├── eval_search.py           골든셋 검색 평가 (hit@k/MRR, 동의어 프로브)
│   └── golden_search.json       검색 평가 골든셋 (25문항)
├── ideation\                 아이데이션 문서 (원본 + rev1)
├── doc\                      개발노트 + 회계동의어 검수표
├── spec_20260601_0937.md     EARS 스펙 (15개 요구사항)
├── requirements.txt          백엔드 의존성
├── prism_fs.spec / build.ps1 PyInstaller 빌드
├── VERSION                   0.1.0
├── .env.example              시크릿 키 이름만
└── (gitignore) storage\ dist\ src\models\ handoff\
```

## 주요 API 엔드포인트
| 분류 | 엔드포인트 |
|---|---|
| 라이브러리 | `POST/GET /api/library`, `GET/DELETE /api/library/{company}/{period}`, `POST /api/library/index/...`, `GET /api/library/index/status` |
| DART 수집 | `POST /api/library/collect`, `GET /api/library/collect/status` |
| 비교조회 | `POST /api/compare` (topic/number 모드, top-5 후보) |
| 재무제표 | `GET /api/fs/{accounts,delta,consolidated-vs-separate,benchmark,ratio,timeseries,flags,consolidated-subtotals}` |
| 신한 인사이트 | `POST /api/coverage`(커버리지 매트릭스), `POST /api/structure-diff`(구조 차집합) |
| 주석 정성분석 | `GET /api/notes/{account-refs,topic-map,compare-memo,rag}` |
| XBRL 진단 | `GET /api/xbrl-tagging`, `GET /api/xbrl-tagging/matrix` |
| 기타 | `GET /api/terms/suggest`, `GET /api/pdf`, `GET /api/health` |

> 모듈 역할·의존관계·데이터 흐름 상세는 [doc/코드구조_20260606.md](doc/코드구조_20260606.md) 참조.

## 스택
| 레이어 | 선택 | 근거 |
|---|---|---|
| 백엔드 | Python 3.11 + FastAPI + uvicorn | DART/임베딩 생태계, prism 직접 이식 |
| 검색 | sentence-transformers(ko-sroberta) + rank-bm25 + kiwipiepy | 오프라인 임베딩+BM25 하이브리드, 형태소 토큰화 |
| PDF | PyMuPDF(fitz) + vendored pdf.js | 본문 추출 + 폐쇄망 뷰어 |
| 프론트 | Vanilla JS 단일 HTML + 순수 CSS | 빌드툴 0, 폐쇄망 친화 |
| 배포 | PyInstaller onedir 풀번들 | torch+모델 동봉, 오프라인 실행 |
