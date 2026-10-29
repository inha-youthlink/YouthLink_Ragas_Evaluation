---
name: ragas-eval-test
description: YouthLink_Ragas_Evaluation(ragas_eval 패키지)의 pytest 테스트 작성 규격. 모듈별로 무엇을 테스트하고 무엇을 빼는지, OpenAI·RAGAS·RAG 서버·DB를 어떻게 대체하는지, 설정 캐시·Decimal·NaN·비동기 함정을 다룬다. ragas_eval 모듈을 추가·수정할 때, tests/에 테스트를 새로 쓰거나 고칠 때, 테스트가 깨질 때 사용한다.
---

# ragas_eval 테스트 규격

## 절차

```
1. 관례 확인   tests/의 기존 테스트 스타일을 읽는다
2. 대상 선별   아래 "모듈별 대상" 표에서 테스트할 동작을 고른다
3. 외부 대체   OpenAI·RAGAS·RAG·DB를 "외부 의존성 대체" 방식으로 바꾼다
4. 작성       AAA 구조, 가짜 데이터로 쓴다
5. 실행       .venv/Scripts/python.exe -m pytest 로 통과 확인
```

## 1. 관례

| 항목 | 규칙 |
| --- | --- |
| 위치 | `tests/test_<모듈>.py` (모듈 1개 ↔ 테스트 파일 1개) |
| 첫 줄 | 한국어 한 줄 역할 주석 (`# dataset 로드·필터 테스트`) |
| 함수 이름 | `test_<조건>_<결과>` 영문 snake_case (`test_run_requires_golden`) |
| 구조 | Arrange / Act / Assert 순서, 빈 줄로 구분, 주석 없음 |
| 단언 | `assert a == b`, 예외는 `with pytest.raises(X)` |
| 여러 입력 | `@pytest.mark.parametrize` |
| 공용 데이터 | `tests/conftest.py`의 fixture, 파일은 `tests/fixtures/` |
| 데이터 값 | 가짜 값만 (`policy_no="TEST-0001"`). 실제 정책·응답 복사 금지 |
| 비동기 | `pytest-asyncio` 없음. `asyncio.run(...)`으로 호출 |

기존 테스트와 스타일이 다르면 기존 쪽을 따른다.

## 2. 모듈별 대상

| 모듈 | 테스트한다 | 테스트하지 않는다 |
| --- | --- | --- |
| `cli.py` | 서브커맨드·기본값·필수 인자 | argparse 자체 |
| `config.py` | 필수 값 누락 시 실패, 기본값 | pydantic 검증 로직 |
| `policy_source.py` | 컬럼 → 원천 텍스트 순서, NULL 컬럼 생략, `metadata`(`policy_no`, `source_updated_at`), 대상 필터 조건 | SQL 문자열 모양 |
| `generate.py` | `reference_contexts` → `policy_no` 매칭, 매칭 실패 처리, jsonl 필드 | `TestsetGenerator` 생성 품질 |
| `dataset.py` | `reviewed = false` 제외, `source_updated_at` 불일치 제외, 필수 필드 누락 | json 파싱 자체 |
| `rag_client.py` | 요청 본문(`question`, `profile`), `?debug=true`, `trace.chunks` → `retrieved_contexts` 순서, HTTP 오류 처리 | 실제 RAG 응답 품질 |
| `scorer.py` | 지표별 인자 전달, `result.value` 매핑, 실패·NaN → `None` + `errors` 기록 | RAGAS 점수 계산 |
| `repository.py` | INSERT·UPDATE 컬럼, 이전 run 미수정, CHECK 위반 시 예외 | psycopg 동작 |
| `runner.py` | 1~5 순서, `--repeat` 횟수만큼 run 생성, 재개 조건(`answer IS NULL`, 지표 전부 NULL), 실패 시 `status = 'FAILED'` | 각 모듈 내부 (mock으로 대체) |

LLM이 만든 결과의 "품질"은 단위 테스트 대상이 아니다. 형식·매핑·흐름만 검증한다.

## 3. 외부 의존성 대체

| 의존성 | 대체 방법 |
| --- | --- |
| RAGAS 지표 | 지표 객체를 `unittest.mock.AsyncMock`으로 바꾸고 `ascore.return_value = SimpleNamespace(value=0.8)` |
| `TestsetGenerator` | 생성 함수를 `monkeypatch`로 바꿔 미리 만든 결과(`user_input`, `reference`, `reference_contexts`, `synthesizer_name`)를 반환 |
| OpenAI 클라이언트 | 생성하지 않는다. 주입 지점에서 mock 전달 |
| RAG 서버 | `httpx.MockTransport(handler)`로 만든 클라이언트를 `rag_client`에 주입 |
| 환경변수 | `monkeypatch.setenv` + `Settings(_env_file=None)` |
| DB (단위) | `repository` 함수를 mock으로 바꾸고 호출 순서·인자만 검증 |
| DB (통합) | 임시 PostgreSQL. `TEST_DATABASE_URL` 없으면 `pytest.skip`. `@pytest.mark.db` 표시 |

RAG 서버 대체 예시는 다음과 같다.

```python
def test_contexts_follow_chunk_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "answer": "가짜 답변",
            "policies": [],
            "trace": {"chunks": [
                {"chunk_id": "00000000-0000-0000-0000-000000000001", "policy_no": "TEST-0001",
                 "chunk_index": 0, "chunk_type": "body", "content": "첫 청크", "score": 0.9},
            ]},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")

    result = asyncio.run(call_pipeline(client, question="가짜 질문"))

    assert result.retrieved_contexts == ["첫 청크"]
```

## 4. 실제 비용·외부 상태를 쓰는 테스트

| 종류 | 마커 | 기본 실행 |
| --- | --- | --- |
| 단위 (mock) | 없음 | 포함 |
| DB 통합 (임시 PostgreSQL) | `@pytest.mark.db` | `TEST_DATABASE_URL` 있을 때만 |
| 실제 OpenAI 호출 | `@pytest.mark.llm` | 제외. 사용자 확인 후 `-m llm`으로만 실행 |

- DB 통합 테스트는 로컬 개발 DB(`DATABASE_URL`)를 쓰지 않는다.
- 통합 테스트는 시작 시 `db/ragas_schema.sql`을 적용하고, 테스트마다 자기가 만든 run만 정리한다.
- 마커를 처음 쓸 때 `pytest.ini`(또는 `pyproject.toml`)에 마커 등록과 `-m "not llm"` 기본값을 함께 추가한다.

## 5. 함정

| 증상 | 원인 | 대응 |
| --- | --- | --- |
| mock이 적용 안 됨 | 패키지(`ragas_eval.runner`)에 patch | 모듈(`ragas_eval.runner.runner`)을 import해 그 속성에 patch |
| 테스트가 실제 키·DB를 읽음 | `Settings`가 `.env`를 읽음 | `Settings(_env_file=None)` |
| 환경변수 바꿔도 반영 안 됨 | `get_settings`의 `lru_cache` | 테스트 전후 `get_settings.cache_clear()` |
| `0.8 != Decimal('0.800')` | `NUMERIC(4,3)`은 `Decimal`로 읽힘 | `Decimal("0.800")`과 비교하거나 `float()` 변환 |
| 점수 저장 시 CHECK 위반·NaN 저장 | RAGAS가 `nan` 반환 | `math.isnan` → `None` 변환을 테스트로 고정 |
| JSONB 저장 실패 | list·dict를 그대로 전달 | `psycopg.types.json.Jsonb(...)`로 감싸기 |
| `RuntimeError: event loop is closed` | 테스트마다 루프 재사용 | 테스트 안에서 `asyncio.run` 한 번만 |
| 한글 깨짐 | Windows 콘솔 인코딩 | 단언은 문자열 비교로 하고 출력은 보지 않는다 |

## 6. 완료 조건

- 변경한 모듈의 테스트 파일이 있고 `.venv/Scripts/python.exe -m pytest`가 통과한다.
- 실제 OpenAI·RAG·개발 DB를 호출하는 코드가 기본 실행에 없다.
- 보고에 실행 결과(통과 수), 건너뛴 테스트(`db`, `llm`)와 이유를 적는다.
