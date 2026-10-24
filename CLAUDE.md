# rag_ragas_evaluation

YouthLink RAG를 RAGAS로 평가하는 저장소다. 서비스 DB의 `policy`로 데이터셋을 만들고,
배포된 RAG를 호출해 RAGAS 지표로 채점한 뒤 결과를 서비스 DB의 평가 테이블에 저장한다.
설계 기준은 `docs/` 아래 PRD 3개(아래 "PRD 구성")이며, 이 문서는 상시 규칙만 정의한다.

## 기술과 구조

- Python 3.12, `ragas` 0.4.x, `langchain-core` 1.x, `langchain-community<0.4`, `psycopg` 3, `httpx`, `pydantic-settings`, `pytest`
- 지표는 v0.4 API(`ragas.metrics.collections`, `await metric.ascore(...)`)를 쓴다.
- 패키지는 `ragas_eval/`, 진입점은 `python -m ragas_eval {generate|run|route|report}`이다.
- 모듈은 `ragas_eval/<모듈>/<모듈>.py` 폴더 단위로 두고 역할은 고정한다. import는 `from ragas_eval.<모듈> import <모듈>`.
  - `config.py`: 환경변수 (최상위)
  - `policy_source/`: `policy` → `Document`
  - `generate/`: `TestsetGenerator` → 후보 jsonl
  - `dataset/`: golden·routing jsonl 로드·검사
  - `rag_client/`: RAG 호출
  - `baseline_client/`: RAG 없이 LLM 직접 호출 (`--baseline`)
  - `scorer/`: 채점
  - `retrieval/`: 검색 적중률 계산 (채점 LLM 없음)
  - `routing/`: 라우팅 평가 채점·집계 (채점 LLM 없음)
  - `repository/`: 평가 테이블 쓰기
  - `runner/`: 실행 순서 조율
  - `report/`: 결과 조회 → 마크다운 성적표 (`results/`)
- 데이터셋은 `datasets/golden_vN.jsonl`(git), 평가 결과는 DB `ragas_evaluation_run`, `ragas_evaluation_samples`에 둔다.
- 평가는 K8s Job으로 실행하고, `generate`는 로컬에서 실행한다.

## 필수 규칙

- 작업 전 `docs/PRD.md`와 작업 영역의 PRD(답변 품질 평가는 `docs/RAGAS_PRD.md`, 검색 적중률·라우팅 평가는 `docs/RETRIEVE_PRD.md`), 관련 코드·테스트를 읽고 최소 범위만 수정한다.
- PRD와 다르게 구현해야 하면 먼저 사용자에게 확인하고, 확정되면 PRD를 함께 갱신한다.
- 새 `.py`·`.sql` 파일 첫 줄에 한국어 한 줄 역할 주석을 단다.
- 설정값은 `config.Settings`로만 읽는다.
- DB 쓰기는 `repository.py`에서만 하고, 대상은 `ragas_evaluation_*` 테이블뿐이다.
- 평가 실행은 run INSERT → samples INSERT → 응답 UPDATE → 점수 UPDATE → 집계 UPDATE 순서를 지킨다.
- 이전 run 행은 수정하지 않는다.
- 지표 계산이 실패하면 값은 NULL로 두고 `metadata.errors`에 기록한다. 0점으로 채우지 않는다.
- RAG 요청·응답 형식은 `docs/PRD.md` "RAG 연동 계약"과 `../YouthLink_RAG/app/schemas/pipeline.py`를 따른다.
- 평가 테이블 스키마를 바꾸면 `db/ragas_schema.sql`, `docs/Schema.md`, `docs/RAGAS_PRD.md` "DB 추가 테이블"을 같은 커밋에서 갱신한다.
- 새 작업 항목은 해당 영역 PRD의 "구현 체크리스트"에 적는다. 공통 기반·배포는 `docs/PRD.md`, 답변 품질은 `docs/RAGAS_PRD.md`, 검색 적중률·라우팅은 `docs/RETRIEVE_PRD.md`.
- 확정된 `golden_vN.jsonl`은 수정하지 않는다. 바꿀 때는 `golden_vN+1.jsonl`을 만든다.
- 테스트에서 OpenAI·RAG 호출은 mock으로 대체한다.
- DB 테스트는 임시 PostgreSQL에서 하고 로컬 개발 DB를 쓰지 않는다.
- 커밋은 논리 단위 하나씩 `type: 한국어 설명` 형식으로 남긴다.

## 금지

- 서비스 테이블(`policy`, `policy_region`, `policy_eligibility_code`, `policy_chunk`, `chat_*`, `message_policy_ref`) 쓰기와 DDL 변경
- `../YouthLink_RAG`, `../youthlink-data-pipeline` 코드를 이 저장소 작업 중에 수정 (필요하면 사용자에게 보고만 한다)
- `.env` 커밋, API 키·DB 비밀번호 하드코딩, 로그·출력에 비밀값 노출
- 실제 LLM API를 호출하는 테스트를 기본 `pytest` 실행에 포함
- 사용자 확인 없는 `generate`·`run` 실제 실행 (LLM 비용 발생)
- `print()` 디버그 출력을 남긴 채 커밋 (CLI 결과 출력은 예외)
- Python 3.14 venv 생성, `langchain-community` 0.4 이상으로 업그레이드
- `DROP`, `TRUNCATE`, 평가 테이블 전체 `DELETE` (예외: 평가 테이블 제약을 다시 정의하는 `ALTER TABLE ... DROP CONSTRAINT IF EXISTS`. 행을 바꾸지 않고 바로 뒤에서 같은 이름으로 다시 만들 때만)
- `git push --force`, `git reset --hard`, `git clean`
- 요청받지 않은 리팩터링, 의존성 추가

## 검증과 완료 보고

- 테스트는 `.venv/Scripts/python.exe -m pytest`로 실행한다.
- 새 기능이나 수정한 기능에는 `tests/`에 단위 테스트를 추가하고 통과를 확인한다.
- `db/ragas_schema.sql`을 바꾸면 임시 PostgreSQL에 두 번 적용해 재실행 안전성과 제약을 확인한다.
- 의존성을 바꾸면 새 3.12 venv에서 `pip install -r requirements.txt`와 `import ragas`를 확인한다.
- 완료 시 변경 내용, 테스트 결과, 미검증 사항, LLM 비용 발생 여부를 보고한다.

## PRD 구성

| 문서 | 내용 |
| --- | --- |
| `docs/PRD.md` | 공통: 평가 종류 3가지(답변 품질·검색 적중률·라우팅), 역할 분담, 전체 흐름, 의존 관계, 현재 상태, RAG 연동 계약, 코드 구조, 명령어, 실행 환경, 선행 작업, 공통 구현 체크리스트 |
| `docs/RAGAS_PRD.md` | 답변 품질 평가 (RAGAS, LLM 채점): 실행 모드(`rag`·`baseline`·`offline`), 데이터셋 생성, 지표 7개(`answer_coverage` 포함), 평가 실행 순서, 보고서, 평가 테이블 DDL |
| `docs/RETRIEVE_PRD.md` | 검색 적중률 평가·라우팅 평가 (코드 채점): 카드 적중(주 지표)과 후보·검색 적중(진단용), Hit@k·MRR(청킹 방식 비교), 라우팅 지표(유형·분석 실패), `routing_vN` 데이터셋 |

코드 주석의 `PRD "섹션명"`은 위 세 문서 중 그 섹션이 있는 문서를 가리킨다.

## 상세 규칙 라우팅

- 서비스 DB 전체 스키마·ERD: `docs/Schema.md`
- 평가 테이블 DDL: `db/ragas_schema.sql`
- 설치·실행 명령: `README.md`
- 테스트 작성: `.claude/skills/ragas-eval-test/SKILL.md`
- 데이터셋 후보 검토·golden 확정: `.claude/skills/ragas-dataset-review/SKILL.md`
- RAG 입출력 모델: `../YouthLink_RAG/app/schemas/pipeline.py`
- 정책 테이블 원본 DDL: `../youthlink-data-pipeline/db/schema.sql`