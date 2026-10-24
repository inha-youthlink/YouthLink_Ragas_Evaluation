# 검색·라우팅 평가 PRD

RAG의 검색(청킹 방식별 top-k 포함)과 질문 라우팅(intent·핸들러)을 코드 기반으로 채점한다. 채점 LLM을 쓰지 않는다.
공통 역할·RAG 연동 계약·실행 환경은 `docs/PRD.md`, 답변 품질(RAGAS) 채점은 `docs/RAGAS_PRD.md`.

## 개요

| 항목 | 검색 평가 | 라우팅 평가 |
| --- | --- | --- |
| 질문 | 정답 정책·필요한 섹션 청크가 상위 k개에 들어오는가 | 질문 분석이 기대 intent를 고르고, 라우터가 기대 핸들러를 고르는가 |
| 요청 | 청킹 방식에 따라 top-k에 적절히 포함되는지 | 질문에 따라 핸들러 라우팅이 잘 되는지 |
| 데이터셋 | golden_v2 (검수 시 기대 청크 종류 추가) | `routing_v1.jsonl` (신규) |
| 비교 대상 | `trace.retrieved_chunks`, `trace.cards` | `trace.analysis.sub_queries[]`, `trace.handler` |
| 채점 | 코드 (정책 번호·청크 종류 비교) | 코드 (값 일치) |
| 비용 | RAG 호출만 (채점 LLM 없음) | RAG 호출만 |

- RAGAS `context_recall`·`context_precision`과의 차이: LLM이 내용으로 판단하지 않고 정책 번호로 판단한다. 매번 같은 결과, 비용 없음. 대신 내용이 같은 다른 정책(동명 정책)을 가져오면 틀린 것으로 센다. 모호한 질문은 검수에서 제외한다
- ragas 0.4 `collections`에는 ID 기반 검색 지표가 없어 직접 계산한다

## RAG 현황 (YouthLink_RAG, 8주차 기준)

| 항목 | 내용 | 평가 영향 |
| --- | --- | --- |
| 질문 분석 | `analyzer` → `trace.analysis` (`sub_queries[]`: `intent`, `standalone_question`, `search_query`, `target_policy_nos`, 지역·나이·취업·학력, `fallback_reason`) | intent 평가 가능 |
| intent | `policy_list`, `policy_detail`, `eligibility`, `comparison`, `chitchat`, `out_of_scope`, `unclear` (`app/schemas/analysis.py`) | 라벨 값 |
| 핸들러 | `trace.handler`는 항상 `default_rag`. 검색·상세·짧은 답변·되묻기 핸들러와 라우터 규칙은 8주차 작업 중 | 핸들러 평가는 라우터 PR 후 |
| 1차 필터 | 질문 조건 + 프로필로 정책을 거른 뒤 검색 (`trace.conditions.filtered_policy_count`) | 질문 조건이 정답 정책을 거르면 검색 불가 |
| 분리 검색 | 지역 조건이 있으면 지역·전국을 각각 `top_k=30`으로 검색해 이어 붙임 (`trace.conditions.split_search`) | `retrieved_chunks` 순서가 단일 순위가 아님 |
| 후보·카드 | 검색 결과에서 후보 6개 → 카드 0~3개 (`trace.cards.candidate_nos`, `selected`) | 검색 → 후보 → 카드 단계별 적중 |
| `contexts` | 후보 정책의 원본 근거 (검색된 청크가 아님) | RAGAS 근거 지표는 후보 선택 평가가 됨 |
| 입력 | `history`, `current_policies` 추가 | 후속 질문·정책 지목 평가 가능 |
| 토큰 | 분석 토큰은 `trace.analysis.tokens`, 생성 토큰은 `trace.tokens` | 지금은 생성 토큰만 저장 |

## 검색 평가

### 데이터셋

golden_v2 검수(`ragas-dataset-review`) 때 필드를 추가한다. 확정된 golden_v1은 수정하지 않는다.

| 필드 | 타입 | 값 | 판단 |
| --- | --- | --- | --- |
| `expected_chunk_types` | list[str] | `SUMMARY`, `ELIGIBILITY`, `SUPPORT`, `APPLICATION` 중 1개 이상 | 질문이 묻는 섹션. 예: 신청 서류 → `APPLICATION`, 대상·나이 → `ELIGIBILITY` |
| `expected_intent` | str | intent 값 | 대부분 `policy_detail`. 예외만 표시 |

- 검수 추가 확인: 질문에 지역·나이 조건이 있으면 정답 정책이 그 조건의 1차 필터를 통과하는지. 충돌하면 "의심"
- 정답 정책은 기존 `policy_no`

### 지표

| 지표 | 단위 | 계산 |
| --- | --- | --- |
| `hit_at_k` (k = 1, 3, 5, 10) | 정책 | 정책 순위 상위 k개에 정답 `policy_no`가 있으면 1 |
| `reciprocal_rank` | 정책 | 정답 정책 순위의 역수, 없으면 0. run 평균이 MRR |
| `chunk_hit_at_k` (k = 5, 10, 30) | 청크 | 상위 k 청크 중 `policy_no` = 정답이고 `chunk_type` ∈ `expected_chunk_types`인 청크가 있으면 1 |
| `candidate_hit` | 후보 | 정답 정책이 `trace.cards.candidate_nos`에 있으면 1 |
| `card_hit` | 카드 | 정답 정책이 카드(`policies`)에 있으면 1 |
| `policies_in_top_k` | 정책 | 상위 `top_k` 청크의 서로 다른 정책 수 (같은 정책 청크가 칸을 얼마나 차지하는지) |

- 정책 순위: 청크를 점수 내림차순으로 정렬한 뒤 정책이 처음 나온 순서
- `retrieved_chunks`가 비면 (검색 없는 핸들러) 해당 샘플 지표 NULL, `errors` 미기록
- `expected_chunk_types`가 없는 샘플은 청크 단위 지표 NULL

### 청킹 방식 비교

| 항목 | 값 |
| --- | --- |
| 방법 | 같은 golden으로 청킹 방식별 run을 따로 실행해 run 평균 비교 |
| 조건 기록 | run `metadata`에 청크 버전, `top_k`, `embedding_model` |
| 청크 적재 | ETL 담당 (`policy_chunk`는 서비스 테이블, 이 저장소에서 쓰기 금지) |

## 라우팅 평가

### 데이터셋 (`datasets/routing_v1.jsonl`)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `sample_id` | str | `route-000001` |
| `question` | str | 사용자 질문 |
| `history` | list | `ConversationTurn` (후속 질문만, 없으면 `[]`) |
| `current_policies` | list | `PolicyRef` (후속 질문만) |
| `profile` | object | `{}` 기본 |
| `expected_intent` | str | intent 값 |
| `expected_handler` | str \| null | 라우터 PR 후 채움 |
| `expected_target_policy_nos` | list[str] | 정책 지목 질문만 ("두 번째 거" → 직전 목록 2번째) |
| `reviewed` | bool | 검수 |

| 구성 | 건수 |
| --- | --- |
| intent 7종 | 각 10~15건 |
| 애매한 질문 (경계 사례) | 10건 내외 |
| 후속 질문 (정책 지목) | 10건 내외 |
| 복합 질문 (하위 질문 2개 이상) | 5건 내외 |

- 작성: Claude가 초안 작성 (LLM API 호출 없음) → 사용자 검수 → 확정

### 지표

| 지표 | 계산 |
| --- | --- |
| `intent_correct` | 하위 질문이 1개이고 `intent` = `expected_intent`면 1 |
| `handler_correct` | `trace.handler` = `expected_handler`면 1 (라우터 PR 후) |
| `target_correct` | `target_policy_nos` 집합 = `expected_target_policy_nos` 집합이면 1 |
| `sub_query_count_correct` | 하위 질문 수가 기대와 같으면 1 (복합 질문) |
| run 집계 | 정확도, intent별 정밀도·재현율, 혼동 행렬 |

- 분석 실패(`fallback_reason` 있음)는 오답으로 세고 사유를 따로 집계

## RAG 계약 변경 대응

| 항목 | 변경 |
| --- | --- |
| 분석 토큰 | `trace.analysis.tokens`를 `metadata.tokens`에 함께 저장 |
| 빈 근거 | `contexts`·`retrieved_chunks`가 빈 응답에서 RAGAS 근거 지표가 0점이 아니라 NULL인지 확인 |
| 입력 | 라우팅 샘플은 `history`, `current_policies`를 함께 보냄 |
| 계약 문서 | `docs/PRD.md` "RAG 연동 계약" 갱신 |

## 결정 필요

| 항목 | 선택지 |
| --- | --- |
| 결과 저장 | ① `ragas_evaluation_samples`에 검색 지표 컬럼 추가 + 라우팅은 새 테이블 `ragas_evaluation_routing` ② 둘 다 `metadata`에만 저장하고 보고서에서 계산 (스키마 변경 없음) |
| 실행 명령 | 검색: 기존 `run`에서 함께 계산 / 라우팅: 새 서브커맨드 `route` (golden과 데이터셋·지표가 달라 분리) |
| 분리 검색 순위 | 점수 내림차순 통합 순위(위 정의) / 지역·전국 따로 집계 |
| 청크 버전 기록 | RAG trace에 추가 요청 / 실행 시 `--chunk-version` 인자로 직접 입력 |

## 팀원 확인 사항

| 대상 | 내용 |
| --- | --- |
| RAG 담당 | 라우터·핸들러 PR 일정, `trace.handler` 값 목록, 분리 검색 순위 해석, 분석 토큰을 `trace.tokens`에 합칠지, 청크 버전을 trace에 넣을지 |
| ETL 담당 | 비교할 청킹 방식 목록과 적재 시점 |

## 구현 체크리스트

- [ ] R-1. "결정 필요" 항목 사용자 확인, 이 문서 갱신
- [ ] R-2. `docs/PRD.md` "RAG 연동 계약"에 분석 토큰·빈 근거·`history` 반영
- [ ] R-3. `GoldenSample`에 `expected_chunk_types`, `expected_intent` (선택 필드) + 테스트
- [ ] R-4. 검색 지표 계산 (`retrieval` 모듈, 순수 함수) + 테스트 (분리 검색, 빈 검색, 동점)
- [ ] R-5. 결과 저장 (R-1 결정에 따라 스키마 또는 `metadata`) + 테스트
- [ ] R-6. `report`에 검색 지표 표 + 테스트
- [ ] R-7. `rag_client`가 분석 토큰 저장 + 테스트
- [ ] R-8. golden_v2 검수에 `expected_chunk_types`·1차 필터 확인 추가 (`ragas-dataset-review` 스킬 갱신)
- [ ] R-9. `routing_v1.jsonl` 초안 → 사용자 검수 → 확정
- [ ] R-10. `route` 서브커맨드 (intent·정책 지목 채점) + 테스트
- [ ] R-11. 라우터 PR 후 `expected_handler` 채우고 `handler_correct` 추가
- [ ] R-12. 실행: golden_v2 검색 지표, `routing_v1` 라우팅 지표 (RAG 호출 비용, 사용자 확인 후)
