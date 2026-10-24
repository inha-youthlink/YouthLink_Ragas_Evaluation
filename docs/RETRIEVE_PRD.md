# 검색 적중률·라우팅 평가 PRD

답변 품질(RAGAS) 외의 두 평가를 다룬다. 둘 다 정답이 정해진 값이라 채점 LLM 없이 코드로 채점한다.
평가 종류 전체·RAG 연동 계약·실행 환경은 `docs/PRD.md`, 답변 품질은 `docs/RAGAS_PRD.md`.

## 한눈에 보기

| 항목 | 검색 적중률 평가 | 라우팅 평가 |
| --- | --- | --- |
| 묻는 것 | 정답 정책을 찾아서 사용자에게 보여 줬나 | 질문 유형을 맞게 분류해 맞는 핸들러로 보냈나 |
| 데이터셋 | `golden_vN` (답변 품질과 같은 데이터셋, 같은 run) | `routing_vN` (따로 만듦, 대화 없는 단일 질문) |
| 정답 | 질문마다 이미 있는 정답 정책 번호(`policy_no`) | 질문마다 적어 둔 기대 유형·핸들러 |
| 실행 | 기존 `run`에서 함께 계산 | 새 명령 `route` |
| 비용 | 추가 없음 (RAG 호출은 답변 품질과 공유) | RAG 호출만 |

## 단계별 지표

질문이 RAG를 거치는 순서대로 단계마다 쓰는 지표를 둔다. 답변 품질 지표 상세는 `docs/RAGAS_PRD.md`.

| 단계 | 하는 일 | 지표 | 응답 값 | 평가 |
| --- | --- | --- | --- | --- |
| 1. 질문 분석 | 질문 유형 분류, 검색어·조건 추출 | `intent_correct`, 분석 실패율 | `trace.analysis.sub_queries[].intent`, `fallback_reason` | 라우팅 (`routing_vN`) |
| 2. 핸들러 선택 | 질문을 맞는 핸들러로 보냄 | `handler_correct` (라우터 구현 후) | `trace.handler` | 라우팅 (`routing_vN`) |
| 3. 1차 필터 | 조건으로 정책 거르기 | `filter_hit` | `trace.filtered_policy_nos` (요청 중) | 검색 적중률 (`golden_vN`) |
| 4. 검색 | 청크 검색, 정책 순위 | `retrieval_hit`, `hit_at_k` (k = 1, 3, 5, 10), MRR | `trace.retrieved_chunks` | 검색 적중률 (`golden_vN`) |
| 5. 후보 | 검색된 정책을 묶고 정렬해서 후보 선택 | `candidate_hit` | `trace.cards.candidate_nos` | 검색 적중률 (`golden_vN`) |
| 6. 카드 | LLM이 후보 중 보여 줄 카드 선택 | **`card_hit`** (검색 적중률 주 지표) | `policies[].policy_no` | 검색 적중률 (`golden_vN`) |
| 7. 근거 전달 | 후보 정책 원문을 LLM에 근거로 넘김 | Context Precision, Context Recall | `trace.contexts` | 답변 품질 (`golden_vN`) |
| 8. 답변 생성 | 근거로 답변 작성 | Faithfulness, Answer Relevancy | `answer`, `trace.contexts` | 답변 품질 (`golden_vN`) |
| 끝에서 끝까지 | 최종 답변이 정답과 원문에 맞나 | Factual Correctness, Answer Coverage, Reference Faithfulness | `answer` (정답·원문은 데이터셋에서) | 답변 품질 (`golden_vN`), `baseline` 비교 |

- 3~6단계는 같은 정답 정책 번호(`policy_no`)로 판정한다. 정답 정책이 어느 단계까지 남았는지 본다
- 마지막 행의 세 지표는 검색 결과를 쓰지 않아 RAG 없는 `baseline`과 같은 기준으로 비교할 수 있다
- 4단계 `hit_at_k`·MRR은 검색기 순위 지표다. 지역·전국 분리 검색 질문은 실제 후보 선택 방식과 순위가 다를 수 있어, 후보 단계 해석은 `candidate_hit`로 한다

## 검색 적중률 평가

### 지표

RAG는 정책을 1차 필터 → 검색 → 후보 → 카드 순서로 좁힌다. 정답 정책이 어디까지 살아남았는지 본다.

| 지표 | 계산 | 용도 |
| --- | --- | --- |
| **`card_hit`** | 정답 정책이 카드(`policies`)에 있으면 1 | **주 지표.** 사용자가 실제로 받은 결과 |
| `candidate_hit` | 정답 정책이 후보(`trace.cards.candidate_nos`)에 있으면 1 | 진단용. 카드에서 빠졌을 때 원인 찾기 |
| `retrieval_hit` | 정답 정책 청크가 검색 결과(`trace.retrieved_chunks`)에 있으면 1 | 진단용 |
| `filter_hit` | 정답 정책이 1차 필터 통과 목록(`trace.filtered_policy_nos`)에 있으면 1 | 진단용. 검색에서 빠졌을 때 필터 탓인지 구분 |
| `hit_at_k` (k = 1, 3, 5, 10), MRR | 검색 결과의 정책 순위에서 정답 위치 | 청킹 방식 비교 |

| 카드 | 후보 | 검색 | 필터 | 원인 |
| --- | --- | --- | --- | --- |
| O | O | O | O 또는 NULL | 정상 |
| X | O | O | O 또는 NULL | 카드 선택 (생성 프롬프트) |
| X | X | O | O 또는 NULL | 후보 선택 (정책 묶기, 정렬) |
| X | X | X | O 또는 NULL | 검색 (청킹, 임베딩, 검색어) |
| X | X | X | X | 1차 필터 (조건 추출, 정책 조건 데이터) |

### 규칙

- 단계 경계(검색 개수, 후보 수)는 코드에 고정하지 않고 응답에서 읽는다
- 정책 순위는 청크를 점수 내림차순으로 정렬한 뒤 정책이 처음 나온 순서 (지역·전국 분리 검색도 합쳐서 정렬)
- 검색이 없는 응답(빈 `retrieved_chunks`)은 검색·후보 지표를 NULL로 둔다
- `filter_hit`는 필터를 걸지 않았거나(`filtered_policy_nos` = null) 키가 없으면(요청 반영 전 RAG) NULL로 둔다. 검색은 통과 목록 안에서만 하므로 `filter_hit` = 0이면 검색·후보·카드도 0이다
- 검색어를 LLM이 다시 쓰므로 같은 질문도 결과가 흔들린다. `--repeat 3` 평균으로 본다
- 결과를 질문에 조건(지역·나이 등)이 있는지로 나눠 보면 1차 필터 영향을 함께 볼 수 있다

### 청킹 방식 비교

같은 `golden_vN`으로 청킹 방식별 run을 따로 돌려 `retrieval_hit`, `hit_at_k`, MRR 평균을 비교한다. 청크 적재는 ETL 담당이 한다 (`policy_chunk`는 서비스 테이블이라 이 저장소에서 쓰지 않음).

## 라우팅 평가

질문 하나를 대화 없이 보내고, 질문 유형과 선택된 핸들러가 기대값과 같은지 본다. 앞 턴(`history`)은 보내지 않는다.

### 지표

| 지표 | 묻는 것 | 계산 | 응답 값 |
| --- | --- | --- | --- |
| `intent_correct` | 질문 유형을 맞게 분류했나 | 하위 질문 유형 묶음 = `expected_intent` 묶음이면 1 (순서 무관, 개수 포함) | `trace.analysis.sub_queries[].intent` |
| `handler_correct` | 맞는 핸들러로 보냈나 | 핸들러 = `expected_handler`면 1 | `trace.handler` (라우터 구현 후) |
| 분석 실패율 | 분석이 실패한 비율 (실패해도 답변은 나옴) | `fallback_reason`이 있는 비율 | `trace.analysis.fallback_reason` |

- 유형 7종: `policy_list`, `policy_detail`, `eligibility`, `comparison`, `chitchat`, `out_of_scope`, `unclear`
- run 집계: 정확도, 유형별 정확도, 혼동 행렬(무엇을 무엇으로 착각했나)
- 유형별 정확도·혼동 행렬은 하위 질문이 1개인 샘플(기대·응답 모두)로만 집계한다. 여러 개인 샘플은 `intent_correct`에만 반영
- 라우터 구현 전에는 핸들러가 `default_rag` 하나라 `handler_correct`를 계산하지 않고 `intent_correct`만 본다
- 이어지는 질문("두 번째 거 알려줘")의 지목·맥락 유지는 평가하지 않는다. 필요해지면 따로 추가

### 데이터셋 (`datasets/routing_vN.jsonl`)

| 필드 | 설명 |
| --- | --- |
| `sample_id` | `route-000001` |
| `question` | 사용자 질문 |
| `expected_intent` | 기대 유형 목록 (list[str], 하위 질문마다 1개. 대부분 1개) |
| `expected_handler` | 기대 핸들러 (라우터 구현 후 채움, 그 전에는 null) |
| `reviewed` | 검수 여부 |

| 구성 | 건수 |
| --- | --- |
| 유형 7종 | 각 10~15건 |
| 하위 질문이 여러 개인 질문 | 5~10건 |

- 작성: Claude가 초안을 쓰고(LLM API 호출 없음) 사용자가 검수
- RAG 분석 프롬프트에 들어 있는 예시 질문은 넣지 않는다 (본 적 있는 질문이라 점수가 높게 나옴)

## 결과 저장

- 계산한 지표는 `ragas_evaluation_samples.metadata`에 저장한다 (스키마 변경 없음)
- 라우팅 run은 `ragas_evaluation_run.mode`에 새 값이 필요하다. 추가할 때 `db/ragas_schema.sql`, `docs/Schema.md`, `docs/RAGAS_PRD.md`를 함께 갱신

## RAG 담당 확인 사항

| 확인 | 이유 |
| --- | --- |
| 평가 대상 서버와 DB | 로컬 DB는 정책당 청크 1개 임시본이라 실제와 다름 |
| 라우터·핸들러 구현 일정, `trace.handler` 값 목록 | `handler_correct` 준비 |
| 질문 유형(`intent`) → 핸들러 대응표 | `expected_handler` 작성 |
| 실제 후보 수 (문서 5, 코드 기본값 6) | 후보 단계 해석 |
| debug 응답 trace 최상위에 `filtered_policy_nos` 추가 (필터 없음 null, 통과 없음 `[]`). 요청 문구는 루트 `요청사항.md` | `filter_hit` 계산 |

## 구현 체크리스트

- [x] R-1. `rag_client`가 후보(`trace.cards`), 카드 정책 번호, 질문 분석, 핸들러, 필터 조건, 분석 토큰을 `metadata`에 저장 + 테스트
- [ ] R-1a. `rag_client`가 `trace.filtered_policy_nos`를 `metadata`에 저장 (키 없으면 생략) + 테스트
- [ ] R-2. 검색 적중률 계산 (`card_hit`, `candidate_hit`, `retrieval_hit`, `filter_hit`, `hit_at_k`, MRR) + 테스트
- [ ] R-3. `run`에서 R-2를 계산해 `metadata`에 저장 + 테스트
- [ ] R-4. 보고서에 검색 적중률 표 + 테스트
- [ ] R-6. `routing_vN` 데이터셋 로드·검사 + 테스트
- [ ] R-7. `route` 명령 (호출 → 라우팅 지표 채점 → 저장) + 테스트
- [ ] R-8. 보고서에 라우팅 표 (정확도, 혼동 행렬) + 테스트
- [ ] R-9. `routing_v1.jsonl` 초안 → 사용자 검수 → 확정
- [ ] R-10. RAG 담당 확인 사항 회신 반영
- [ ] R-11. 라우터 구현 후 `expected_handler`, `handler_correct` 추가
