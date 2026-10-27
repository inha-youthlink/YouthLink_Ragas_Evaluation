---
name: ragas-golden-dataset-review
description: generate로 만든 데이터셋 후보(datasets/golden_candidates.jsonl)에 profile로 프로필을 붙이고, 질문·정답을 정책 원문과 대조하며 프로필을 함께 판정해 1차 검토한 뒤, 사용자 승인 후 golden_vN.jsonl로 확정하는 절차와 판정 기준. 후보 검토·검수표 작성·의심 샘플 표시·golden 확정을 요청받을 때, 팀원 검토 의견을 판정 기준에 반영할 때 사용한다.
---

# golden 데이터셋 후보 검토

routing 데이터셋은 `ragas-routing-dataset` 스킬을 따른다.

## 절차

```
1. 준비       후보 건수·필드 확인, 확정본 버전(N) 결정, 후보에 profile 적용 (검수 대상·이유 저장)
2. 전수 대조   후보마다 질문·정답을 reference_contexts(정책 원문)와 대조, 프로필 검수 대상 확인
3. 판정       아래 "판정 기준"·"프로필 판정"으로 유지 / 의심 / 제외 추천 표시
4. 검수표     docs/golden_vN/review.md 작성 (로컬 전용, 커밋하지 않음)
5. 승인       사용자에게 요약·제외 후보를 보고하고 결정을 받음
6. 확정       승인된 건만 datasets/golden_vN.jsonl로 저장, reviewed=true, 커밋
```

## 0. 규칙

| 항목 | 규칙 |
| --- | --- |
| 비용 | 검토는 Claude가 직접 읽어서 한다. OpenAI 등 LLM API를 호출하지 않는다 |
| 확정본 | 이미 있는 `golden_vN.jsonl`은 수정하지 않는다. 바꿀 때는 `golden_vN+1.jsonl` |
| 정답 수정 | 후보 단계에서도 정답·근거를 임의로 고치지 않는다. 고칠 필요가 있으면 제외하거나 사용자에게 묻는다 |
| 프로필 수정 | 프로필(`region_code`·`age`)은 정답이 아니라 RAG 입력이라 "프로필 판정"에 따라 고칠 수 있다. 고친 값과 이유를 검수표에 남긴다 |
| 승인 | 확정(6단계)은 사용자가 결정을 준 뒤에만 한다. 사용자가 직접 검수하지 않았는데 검수했다고 기록하지 않는다 |
| sample_id | 후보의 sample_id를 그대로 유지해 검수표 번호와 대응시킨다 |
| 기록 | 판정 기준을 바꾸거나 제외 결정을 내리면 `docs/golden_vN/context-notes.md`에 이유를 남긴다 |

## 1. 준비

```bash
# 후보를 번호·질문·정답·원문 순으로 펼쳐 읽기 쉽게 만든다 (scratchpad에 저장)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
import json, sys
rows = [json.loads(l) for l in open('datasets/golden_candidates.jsonl', encoding='utf-8')]
out = open(sys.argv[1], 'w', encoding='utf-8')
for i, r in enumerate(rows, 1):
    out.write(f'##### {i} {r[\"sample_id\"]} {r[\"policy_no\"]}\nQ: {r[\"question\"]}\nA: {r[\"ground_truth\"]}\nCTX:\n' + '\n'.join(r['reference_contexts']) + '\n\n')
" <scratchpad>/review_dump.txt
```

후보에 프로필을 붙인다 (LLM 없음, DB 읽기만). 후보는 원래 모두 `reviewed = false`라 검수 대상은 명령이 출력한 이유 목록으로 구분하므로 출력을 저장한다.

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ragas_eval profile --golden datasets/golden_candidates.jsonl > <scratchpad>/profile_flags.txt
# 이후 대조·확정은 datasets/golden_profile_candidates.jsonl 기준
```

프로필 없이 이미 확정된 `golden_vN`에 붙일 때는 `--golden datasets/golden_vN.jsonl`로 실행하고 `golden_vN+1`로 확정한다 (질문·정답 대조는 생략하고 프로필 판정만 한다).

기계적으로 먼저 잡을 수 있는 것도 확인한다.

| 확인 | 방법 |
| --- | --- |
| 동명 정책 | 원문 첫 줄(`정책명:`)이 같은 후보 묶기. 후보끼리만 보면 해마다 다시 등록된 정책·전국판을 놓치므로 아래 DB 조회도 한다 |
| 메타 문구 | 정답에 `context`, `컨텍스트`, `제공된 내용`이 있는지 |
| 정책 중복 | 같은 `policy_no`가 여러 건인지 |

```sql
-- 정답 정책과 이름이 같은 DB 정책 (공백·괄호·앞 연도 제거 후 비교, 읽기 전용)
-- 지역(policy_region), 기관(supervising_org_name), 등록 연도(policy_no 앞 4자리)를 함께 본다
SELECT p.policy_no, p.policy_name, p.supervising_org_name, array_agg(r.region_code) AS regions
FROM policy p LEFT JOIN policy_region r USING (policy_no)
WHERE regexp_replace(regexp_replace(p.policy_name, '\([^)]*\)|^\d{4}년\s*', '', 'g'), '\s', '', 'g')
    = regexp_replace(regexp_replace(%(name)s, '\([^)]*\)|^\d{4}년\s*', '', 'g'), '\s', '', 'g')
GROUP BY 1, 2, 3;
```

## 2–3. 판정 기준

후보마다 원문을 끝까지 읽고 아래 항목을 확인한다.

| 유형 | 판정 | 기준 | 예 |
| --- | --- | --- | --- |
| 모호 | 제외 추천 | 질문만으로 정책을 특정할 수 없음. 지역명·정책명이 없고 같은 종류 정책이 여러 지역에 있음. 정답 정책만 맞게 채점되어 RAG가 불리 | "청년기업 융자지원사업", "인천 청년창업 지원 뭐 있어?" |
| 정답 불일치 | 제외 추천 | 정답의 금액·연령·기간·대상이 원문과 다름 | |
| 원문 오류 반영 | 제외 추천 | 원문의 오타·모순·API 기본값이 정답에 그대로 들어감 | "26026년", "만 1세~99세", 연령이 두 가지로 적힘 |
| 칸 불일치 | 의심 | 원문 항목에 맞지 않는 값이 들어 있고 정답이 그대로 옮김 | "소득 기준: 도내 주민등록 무주택 청년" |
| 메타 문구 | 의심 | 정답에 "context에는", "제공된 내용에 없음" 같은 생성기 말투 | |
| 동명·유사 정책 | 제외 추천 | 같은 이름(정규화 후)의 다른 정책이 DB에 있고, 질문에 그 차이를 구분할 단서(지역·기관·연도)가 없음. 검색 적중률은 정책 번호로 채점해 내용이 같은 다른 번호를 가져오면 오답이 됨. 단서가 있으면 유지, 후보 안의 같은 정책 쌍은 1건만 남김 | 지역만 다른 "청년 자격증 시험 응시료 지원 사업", 해마다 다시 등록된 "대학생 천원의 아침밥 사업" |
| 페르소나 불일치 | 제외 추천 | 질문자 설정이 정책 대상과 맞지 않음. RAG 1차 필터가 질문의 조건(지역·나이·취업 상태·학력)으로 정답 정책을 먼저 뺄 수 있어, 정답이 대상 여부를 밝혀도 제외 | 창업자 + 미취업자 사업, 신혼부부 + 청년센터 |
| 시점 의존 | 의심 | 지난 일정, "지금 접수 가능?", "2월 중 취급 예정"처럼 시점이 지나면 틀리는 내용 | |
| 마감 정책 | 의심 | 원문에 마감 표시가 있거나 신청 기간이 기준일 이전. 생성 대상에서 빠져야 하므로 나오면 `policy_source` 대상 조건도 확인 | |
| 원문 없음 | 의심 | 질문이 원문에 없는 정보를 물음 | 원문에 없는 지원금 질문 |
| 단순 | 의심 | 답이 한 줄이거나 질문에 답이 들어 있음 | "소득 무관 맞아?" → "네" |
| 개인 대상 아님 | 의심 | 건축비·기관 운영비처럼 사용자가 신청할 수 없는 사업을 사용자 관점으로 물음 | |

문제가 없으면 유지한다. 일부러 넣은 오타·구어체("어케", "잇나요")와 검색어형 질문은 문제로 보지 않는다.

### 프로필 판정

`profile_flags.txt`에 이유가 나온 샘플만 본다. 프로필은 "정답 정책이 1차 필터를 통과하고, 질문의 지역·나이와 어긋나지 않는 사용자"여야 한다.

| 이유 | 판정 | 처리 |
| --- | --- | --- |
| 질문에 `NN세`가 있음 | 자격 범위면 유지, 사용자 본인 나이면 `age`를 그 나이로 고침 | 고친 나이가 정책 나이 범위 밖이면 제외 추천 |
| 정답이 자격 없음 판정이라 프로필을 비움 | 정답이 정말 "대상이 아니다"인지 원문과 대조 | 맞으면 프로필 없이 유지, 아니면 정책 조건에 맞는 지역·나이를 채움 |
| 지명을 시도로 정할 수 없어 지역을 비움 | 지명이 아닌 단어를 오인했으면 정책 지역 안의 시도로 채움 | 실제 시군구면 그 시도 전체 코드(`XX000`) |
| 질문의 시도가 정책 지역 밖이거나 여러 개 | 질문과 정답 정책이 어긋남 | 제외 추천 (모호 유형과 같음) |
| 질문의 나이대가 정책 나이 범위 밖 | 질문자가 정책 대상이 아님 | 제외 추천 (페르소나 불일치와 같음) |
| 나이대를 정책 범위에 맞춰 바꿈 | 바뀐 나이가 질문의 나이대 안이면 유지 | |
| DB에 정책 없음, 정책 나이 하한 > 상한 | 데이터 문제 | 제외 추천 |


## 4. 검수표 (`docs/golden_vN/review.md`)

```markdown
# golden_vN 검수표

## 요약
| 1차 판정 | 건수 | 의미 |
(유지 / 의심 / 제외 추천 건수, 목표 건수까지 몇 건을 더 빼야 하는지)

## 의심 유형
| 유형 | 내용 |

## 전체 목록
| # | sample_id | 정책명 | 질문 | 프로필 | 1차 판정 | 유형 | 이유 | 최종 |
```

- 표 셀의 `|`는 `\|`로, 줄바꿈은 공백으로 바꾼다.
- 팀원에게 질문만 검수받을 때는 `docs/golden_vN/question_review.md`에 번호·sample_id·정책명·질문·판정·메모만 담는다. 정답이 원문 대조를 마쳤다는 안내와 일부러 넣은 오타는 문제가 아니라는 안내를 맨 위에 쓴다.

## 5. 승인 요청

사용자에게 다음을 표로 보고한다.

| 항목 | 내용 |
| --- | --- |
| 요약 | 유지 / 의심 / 제외 추천 건수 |
| 제외 추천 | 번호, 질문, 이유 |
| 의심 | 유형별 건수, 예시, 제 의견 |
| 추천안 | 목표 건수를 맞추는 제외 목록. 우선순위: 제외 추천(모호·동명 구분 불가·페르소나 불일치 포함) → 정답 정확성 → 시점·원문 없음·메타 문구·단순 |

## 6. 확정

```python
# golden_profile_candidates.jsonl(프로필 붙은 후보)에서 승인된 제외 번호(EXCLUDED)를 빼고,
# 고친 프로필(PROFILE_FIXES: sample_id → profile)을 반영해 golden_vN.jsonl을 만든다. 확정본이 있으면 중단
assert not out.exists()
kept = [dict(r, profile=PROFILE_FIXES.get(r["sample_id"], r["profile"]), reviewed=True) for i, r in enumerate(rows, 1) if i not in EXCLUDED]
out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8", newline="\n")
```

- `ragas_eval.dataset.dataset.load_golden`으로 다시 읽어 건수, `reviewed`, `policy_no` 중복, 프로필 형식을 확인한다.
- 검수표 최종 열에 O/X를 채운다.
- `datasets/golden_vN.jsonl`만 커밋한다 (`feat: 검수 완료 데이터셋 golden_vN M건 확정`). `docs/golden_vN/`은 커밋하지 않는다.

## 7. 피드백 반영

팀원 의견이 오면 유형을 나눠 처리한다.

| 의견 유형 | 처리 위치 |
| --- | --- |
| 정답에 사실 정보가 빠짐·틀림 | 정책 문서 변환 (`ragas_eval/policy_source/policy_source.py`) |
| 질문·정답의 작성 방식 (말투, 판단, 구성) | 생성 프롬프트 (`ragas_eval/generate/generate.py`) |
| 위 둘로 못 거르는 것 | 이 skill의 판정 기준에 추가 |
| 질문 문장만 고치면 되는 것 | 다음 버전(`golden_vN+1`)에서 묻는 내용은 그대로 두고 말투만 고침 |
