# 아이데이션 — 4대 금융지주 주석·재무제표 비교 대시보드 (prism-fs)

> 작성: 2026-06-01 09:37 · 소유자: 신한금융지주 회계팀(hrlee@shinhan.com)
> 환경: Windows 11 + PowerShell, 폐쇄망 데모 PC · 산출물: 단일 HTML 대시보드 + (후속)Python 백엔드
> **개정(rev1): 2026-06-01 · AI 활용 전략(정성/주석 영역) 반영 — §5 신설, §4 P2 정식화 포인터 추가**

---

## 1. 배경·목표

신한 회계팀이 4대 금융지주(신한·KB·하나·우리)의 **연결(CFS)/별도(OFS)** 재무제표와
주석을 한 화면에서 검색·비교하고, 재무제표 비교분석 업무를 돕는 **독립 신규 웹 대시보드**를 요청.

기존 `prism`(D:\SOURCE\prism)의 검증된 자산을 **참조·이식**하되 새 레포에서 시작한다.

### 확정 요구 3가지
1. 년도·분기별 4사의 **주석·주기(회계정책)·서술형 주석 검색·비교조회**
2. 라이브러리 매트릭스에 **연결·별도 보고서 둘 다** 수집 (연결/별도가 회사×기간과 함께 1급 차원)
3. **회계담당자 재무제표 비교분석 기능** 추천·포함

---

## 2. 사전 조사

### 2.1 구현 방안 비교 (3안)

| 방안 | 내용 | 장점 | 단점 | 판정 |
|---|---|---|---|---|
| **A. prism 이식형** | prism 백엔드(collect_dart/compare/coverage/structure-diff)를 새 레포로 복사 후 연결/별도 차원만 일반화 | 검증된 코드 재사용, 빠른 MVP, 안전경계 이미 내재 | prism 부채(미정리 코드) 일부 유입 | ✅ **채택**(백엔드) |
| B. 신규 설계형 | 데이터모델부터 새로 설계 | 깔끔한 구조 | 재무추출·임베딩·DART 수집 재구현 비용 큼, 리스크 | 기각 |
| C. prism 직접 확장 | prism 레포 안에 탭 추가 | 가장 빠름 | "독립 신규 프로젝트" 요구 위배, 결합도↑ | 기각(사용자 결정) |

### 2.2 매트릭스 차원 모델 (핵심 설계)

연결/별도를 1급 차원으로 승격:
```
Cell = company{신한,KB,하나,우리}
     × period{YYYYQ#, YYYYFY}
     × fs_div{연결=CFS, 별도=OFS}
     × kind{재무제표, 주석(report|review)}
```
- **재무제표**: `fs_structured.json.by_fs_div[CFS|OFS]` 직접 매핑 — **신규 수집 불필요**(이미 양쪽 적재).
- **주석**: index.json note의 `fs_div`("연결"/"별도") 태그를 차원 키로 승격. report/review 병존.

### 2.3 제약사항 + 극복방안

| 제약 | 내용 | 극복방안 |
|---|---|---|
| **폐쇄망 CDN** | 데모 PC가 외부 CDN 차단 가능(앞선 Mermaid CDN 차단 사례·prism pdf.js cdnjs 의존도 리스크) | 외부 CDN 0. 순수 CSS + 인라인 JS, pdf.js/임베딩 모델 **vendored 동봉**. 회귀 테스트로 강제 |
| **bfefrmtrm 부재** | BS의 전전기 금액 `bfefrmtrm_amount`가 사실상 비어있음(실측 1/44) | 단일 셀로 2년 전 추론 금지. 시계열은 **여러 기간 셀 교차 로드** |
| **비교 컬럼 상이** | BS는 당기말 vs 전기말, 손익(CIS)은 당기누계 vs 전기동기로 필드가 다름 | sj_div별 비교 컬럼 **자동 선택**(BS=frmtrm, CIS=frmtrm_q) |
| **본문 PDF 수급** | 별도 주석 텍스트는 분기보고서 본문 PDF 필요(검토보고서는 연결 중심) | prism `collect_dart.py --report-pdf`(attach_files 경로) 재사용 |
| **단위 상이** | 회사별 표기 단위(원/백만원) 차이 | 환산 금지. 단위 불일치 경고 배너 + 원문 표기 유지 |
| **계정명 불일치** | 4사 account_nm 표기 차이(예: 분기순이익 vs 당기순이익) | account_id(IFRS 표준) 기준 매칭 + 한쪽만 존재 시 "대응 없음" 플래그 |

### 2.4 검증된 데이터 사실 (실측)
- `fs_structured.json` = `{by_fs_div:{CFS,OFS}}`, account에 `sj_div(BS/CIS/CF/SCE)`, `account_id`, `thstrm/frmtrm/frmtrm_q/bfefrmtrm` 원문.
- **BS**: thstrm(당기말 44/44) + frmtrm(전기말 44/44) → YoY 가능. bfefrmtrm 1/44, frmtrm_q 0/44.
- **CIS(손익)**: thstrm(당기누계 50/50) + **frmtrm_q(전기동기 50/50)**, frmtrm 0/50.
- CFS/OFS 양쪽 = 4사 × 2~3기간 = **10셀** 적재(연결 325~414계정/별도 148~209계정).

---

## 3. 언어/스택 선정

### 선정 결과 (CLAUDE.md 언어선정 규칙 적용)

| 레이어 | 언어/도구 | 근거 |
|---|---|---|
| 데이터/AI(수집·구조화·검색) | **Python 3.11** | 데이터 처리/AI = Python(생태계). DART OpenAPI·임베딩·BM25, prism 직접 재사용. py3.11=OpenDartReader 0.2.x 호환(0.3은 py3.13 요구) |
| 백엔드 API | **FastAPI + uvicorn** | prism main.py 그대로 이식 가능 |
| 웹 UI | **Vanilla JS 단일 HTML + 순수 CSS** | 웹 UI=JS(CLAUDE.md). 빌드툴 0 → 폐쇄망 친화. Tailwind/CDN 미사용 |

### 언어 선정 필수 검토 체크리스트
- [x] **Windows 11 빌드/실행**: Python 3.11 + FastAPI, 시스템 Python 검증됨(prism 동일 스택)
- [x] **dist\setup\ 패키징**: PyInstaller onefile + static 동봉 → `setup_v{VERSION}.exe`(추후)
- [x] **장기 유지보수**: prism 와 동일 스택 → 팀 학습비용 0, 자산 공유
- [x] **핵심 라이브러리 Windows 호환**: fastapi/uvicorn/lxml/opendartreader/sentence-transformers 모두 Windows wheel 제공

### 폐쇄망 의존 전략 — vendored 채택
- **CSS**: Tailwind CDN 금지 → 순수 CSS 1파일(prism 인라인 `<style>` 톤 이식, CSS 변수 토큰)
- **pdf.js**: cdnjs import 제거 → `src/static/vendor/pdfjs/` 동봉, 상대경로 import
- **임베딩 모델**: ko-sroberta 가중치 동봉 + `HF_HUB_OFFLINE=1`. 부재 시 bigram 폴백
- **LLM**: 기본 비활성, .env 키 옵트인 시 로컬 Ollama
- **회귀 게이트**: `tests/test_no_external_cdn.py` — static HTML에 외부 `https://` 참조 0건 강제

---

## 4. 회계담당자 비교분석 기능 카탈로그 (우선순위순)

> **공통 안전경계**: 모든 파생은 `src/fs_compare.py` 결정론 함수만. 각 결과에
> `{value, formula, inputs:[{account_id, raw_amount, period, fs_div}], engine:"deterministic"}`
> provenance 동봉. AI/LLM 무경유. 원문 라인(진함)과 파생 라인(회색) 시각 분리. 단위 환산 없음.

### P1 (MVP — 프로토타입에 시연)

**① 계정 시계열 증감 (QoQ/YoY)**
- 무엇: 같은 회사·fs_div·account_id를 여러 기간 셀에 걸쳐 증감액·증감률 표시
- 입력: 2개 이상 기간 셀의 `thstrm`(+비교컬럼)
- 계산식: `Δ = 금액(T) − 금액(T-1)`, `증감률 = Δ / |금액(T-1)|` (분모 0 → N/A)
- 안전경계: 비교 컬럼 sj_div별 자동선택(BS=frmtrm, CIS=frmtrm_q). **bfefrmtrm 단일셀 추론 금지**
- UI: 계정행 + 기간열, Δ/% 회색 보조열, ⓘ 계산식 팝오버

**② 연결 vs 별도 차이 (자회사 효과)**
- 무엇: 동일 회사·기간 account_id의 `CFS − OFS` → 자회사 기여 규모(추정)
- 계산식: `차이 = 연결금액 − 별도금액` (라벨 "자회사효과(추정·연결조정 미반영)")
- 안전경계: 한쪽만 존재 시 "대응 없음" 플래그(차감 안 함)
- UI: 연결·별도·차이 3열 + 추정 워터마크

**③ 4사 벤치마킹**
- 무엇: 동일 기간·fs_div 4사 동일 account_id 나란히 + 순위
- 계산식: 정렬·순위만(원문 표기 유지)
- 안전경계: detected_unit 상이 시 단위 경고 배너, 자동 환산 금지
- UI: 4열 비교표

**④ 안전 구조비율**
- 무엇: 부채비율(부채/자본), 자기자본비율(자본/자산) 등
- 계산식: 분자·분모가 **원문 account_id로 둘 다 존재할 때만** 계산, formula 동봉
- 안전경계: BIS 등 산식 비공개 비율 **불포함**. 미존재 시 "계산 불가"
- UI: 비율 + ⓘ "분자÷분모 = ?" 근거

### P2 (후속)
- **⑤ 주석↔재무제표 정합 참조**: account_nm↔주석번호 후보를 임베딩으로 제시, 확정은 사용자. 숫자 자동 일치판정 금지 — **(→ §5.2 표준 주제 매핑으로 정식화)**
- **⑥ 이상치/누락 플래그**: 결정론 룰(부호 반전·N배 급변·셀 누락·단위 불일치·대응 없음). "확인 필요" 톤
- **⑦ 회계정책(주기) diff**: 동일 주제 주석 회사/기간 병치. 요약은 LLM 옵트인 시에만 — **(→ §5.3 정책·가정 차이 비교로 정식화)**
- **⑧ 연결조정 추정 상세**: ②를 sj_div 합계까지 확장. "추정·비감사" 워터마크

---

## 5. AI 활용 전략 — 정성(주석) 영역  *(rev1 신설)*

> **설계 제1원칙: 정량(결정론) ↔ 정성(AI) 분리.**
> 이 업무의 병목은 숫자가 아니라 **4사가 서로 다른 형식·용어로 쓴 주석**이다.
> 숫자는 §4 결정론 엔진(DART API/XBRL)이 유일한 source of truth — AI는 숫자를 **읽거나 생성하지 않는다**.
> AI는 서술형 주석의 **분류·정규화·비교초안·질의응답**에만 관여하며, 모든 산출물은
> ① 원문 인용(보고서·주석번호·페이지) 강제 ② human-in-the-loop 검증 ③ .env 옵트인.
> 이 세 경계가 빠지면 감사 맥락에서 효율이 아니라 리스크가 된다.

### 5.1 수집 — 숫자는 AI 무경유
- 정량 골격: **다중회사 주요계정 API + 재무제표 원본파일(XBRL)** → 결정론 적재(§2.4 `fs_structured.json` 그대로).
- AI 입력 대상은 **주석 텍스트·표뿐**. PDF 표에서 숫자 추출을 LLM에 위임 금지(자릿수·환각 리스크). 표 구조 추출은 레이아웃 인식 파서(prism `--report-pdf` 경로 + 후속 layout 파싱)가 담당.

### 5.2 주석 구조화 — AI의 본 영역 (P2-⑤⑦ 승격)
- **표준 주제 매핑**: 회사마다 번호·제목이 다른 주석(예: "주석7. 대출채권" ↔ "주석9. 상각후원가 측정 금융자산")을 canonical topic(예: *여신/대출채권*)으로 분류.
- **용어 정규화**: 대손충당금↔신용손실충당금, 공정가치 Level 표기 차이 등을 **통제 어휘집 + 임베딩 유사도**로 통일.
- 산출 스키마: `(company, period, fs_div, topic, 추출수치[provenance], 정책서술, 출처페이지)` → 이후 비교는 전부 쿼리로 해결.

### 5.3 비교 — "무엇이 다른가" AI 초안 (옵트인)
회계담당자가 실제로 보고 싶은 것은 숫자 차이보다 **회계정책·추정 가정의 차이**:
- ECL(기대신용손실) 산정 시 forward-looking 시나리오 가중치 공시 여부
- 충당금 추정 방법론 / 공정가치 Level 3 투입변수·민감도
- 우발부채·특수관계자 거래 범위
- 연결범위(종속기업) 변동
- 전기 대비 변경점(QoQ/YoY) → **AI가 선플래그, 사람이 검증**. 숫자 변화 자체는 §4-① 결정론 결과를 **인용만** 한다(AI 재계산 금지).

### 5.4 검토 보조 — 주석 RAG (옵트인)
- 4사 × 기간 × (연결/별도) 주석 전문 인덱싱 → 자연어 질의
  (예: *"4사 중 Level 3 공정가치에 민감도 분석을 공시한 곳과 그 가정은?"*)
- **출처(보고서·주석번호·페이지) 동반 반환 강제** — 인용 없는 답변은 미채택.
- 최종 단계: **비교 메모 초안** 생성 → 담당자는 '작성'이 아니라 '검증·판단'에 시간을 쓴다.

### 5.5 시간 절감이 실제로 나오는 지점
수집·표추출·용어통일·주제정렬·초안작성이 자동화되고, 사람은 **이상치 검증과 회계적 판단**(고부가)에 집중. 효율의 본질은 작업 제거가 아니라 **저부가 작업의 이전**.

### 5.6 prism-fs 안전경계와의 정합
- §4 공통 안전경계(결정론·provenance·단위환산 금지)는 **숫자 레이어**에서 불변 유지.
- 본 AI 레이어는 **텍스트 레이어 전용** — LLM 기본 비활성(§3 vendored 전략)·로컬 Ollama 옵트인 그대로.
- 회귀 게이트에 **AI 산출물 인용 누락 검출** 테스트 추가 권고(`tests/test_ai_citation_required.py`).

---

## 6. 단일 HTML 대시보드 레이아웃

| 탭 | 내용 | 프로토타입 |
|---|---|---|
| 📚 라이브러리 매트릭스 | 4사×기간, 셀당 4상태칩 `[연결-FS][별도-FS][연결-주석][별도-주석]` | 데모 상태표시 |
| 📊 재무제표 비교(핵심) | 셀 선택 + 서브탭 [시계열 증감][연결vs별도][4사 벤치마킹][구조비율]. provenance 팝오버 | **실데이터 클릭 가능** |
| 🔍 주석 검색·비교 | fs_div 셀렉터 + 주기/서술형 필터 (+ §5.4 RAG 질의·인용, 옵트인) | 샘플 + "백엔드 연동" 배지 |
| 🧭 커버리지/공시갭 | coverage 매트릭스 + structure-diff | 데모(다음 단계) |

공통: 원문=진한 라인 / 파생=회색 보조 라인 + ⓘ 계산식, 단위 경고 배너, 안전경계 푸터.
AI 산출(요약·비교초안·RAG 답변)은 **별도 톤 + 인용 칩**으로 결정론 결과와 시각 분리.

---

## 7. PoC 범위

### MVP (이번 세션 — 기획 + UI 프로토타입)
- 프로젝트 스캐폴드, 아이데이션 문서, EARS 스펙, 오프라인 클릭 가능 단일 HTML(실데이터 임베드)

### 다음 세션 (백엔드 동작 MVP)
- prism 이식: `collect_dart.py`(CFS/OFS 루프 그대로), `notes_search.py`(compare/coverage/structure-diff + `_notes_for_comparison` **fs_div 파라미터화**), `catalog.py`(매트릭스 fs_div 차원), 신규 `fs_compare.py`(P1 계산 코어)·`safety.py`
- pdf.js vendored, 임베딩 오프라인, 안전 게이트 테스트(`test_no_external_cdn.py`, `test_fs_compare.py`)

### 후속
- P2 기능 → LLM 옵트인 요약(Ollama) → 영문 Word/KG 통합 → PyInstaller `dist\setup\` + Playwright E2E
- **AI 정성 레이어(§5)**: 표준 주제 매핑·용어 정규화 파이프라인 → 주석 RAG(인용 강제) → 비교 메모 초안. 전 단계 **옵트인·human-in-the-loop**, `test_ai_citation_required.py` 게이트 동반

---

## 8. prism → prism-fs 이식 매핑

| 원본(prism) | 대상 | 처리 |
|---|---|---|
| `backend/collect_dart.py` (collect_company CFS/OFS 루프) | `src/collect_dart.py` | 그대로 복사 |
| `main.py` compare/coverage/structure-diff + `_notes_for_comparison`/`match_query_in_notes` | `src/notes_search.py` | 이식 + fs_div 파라미터화 |
| `main.py` `/api/library*` + catalog | `src/catalog.py` | 이식 + fs_div 차원 |
| `backend/static/index.html` 탭·CSS | `src/static/index.html` + `app.css` | 이식, **pdf.js CDN→vendored** |
| (신규) | `src/fs_compare.py`, `src/safety.py` | P1 계산 코어 · 마스킹/provenance 중앙화 |
| (신규·옵트인) | `src/note_topics.py` | §5.2 표준 주제 매핑·용어 정규화 |
| (신규·옵트인) | `src/notes_rag.py` | §5.4 주석 RAG·인용 강제 반환 |
