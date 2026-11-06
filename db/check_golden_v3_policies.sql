-- golden_v3 정답 정책이 지금 DB에서 RAG 검색·필터를 통과할 수 있는지 점검 (SELECT만, LLM 비용 없음)
--
-- 언제: ETL 초기 적재(정책·청크·임베딩)가 끝난 뒤, evaluate 전에 실행한다.
-- 판정 기준은 RAG 코드와 같다.
--   검색 제외: policy.is_active = FALSE (vector_repository), 임베딩 모델이 bedrock-titan-embed-v2인 청크만 검색
--   1차 필터: 나이(min_age·max_age), 지역(v_policy_region_resolved.sido_code), "지금 신청 가능" 질문이면 마감 제외
--   재정렬: 마감 0.05·예정/정보 없음 0.025 감점, 같은 정책 이름은 점수가 높은 하나만 후보에 남김
-- 신청 상태는 RAG application_status()와 같은 순서로 판정하고, 기준 날짜는 한국 시간 오늘이다.
-- "지금 신청 질문"은 질문 문장의 표현으로 추정한 값이다 (RAG 질문 분석 결과와 다를 수 있음).
--
-- 판정 (위에서부터 우선)
--   제외: 정답 정책이 DB에 없거나 비활성 → v3에서 샘플을 뺀다
--   적재 확인: 임베딩된 청크가 없음 → ETL 임베딩 단계를 먼저 확인한다
--   프로필 재생성: 프로필 나이·지역이 정책 조건과 안 맞음 → profile 명령으로 다시 붙인다
--   질문 확인: 마감 정책인데 "지금 신청 가능" 질문 → 1차 필터에서 빠지므로 빼거나 고친다
--   참고: 마감·예정·정보 없음 감점, 같은 이름의 다른 활성 공고 → 순위가 밀릴 수 있다 (그대로 평가 가능)
--   정상
WITH golden (sample_id, policy_no, age, region_code, open_only_question) AS (
    VALUES
        ('single-000001', '20240109005400100003', 25, '11000', FALSE),
        ('single-000003', '20250103005400210042', 28, '12000', FALSE),
        ('single-000004', '20250110005400210146', 25, '28000', FALSE),
        ('single-000005', '20250114005400110246', 25, '27000', FALSE),
        ('single-000006', '20250114005400210222', 25, '26000', FALSE),
        ('single-000007', '20250114005400210225', 25, '26000', FALSE),
        ('single-000009', '20250115005400210291', 25, '50000', FALSE),
        ('single-000011', '20250117005400210324', 25, '26000', FALSE),
        ('single-000012', '20250123005400110393', 25, '41000', FALSE),
        ('single-000013', '20250212005400110422', 25, '43000', FALSE),
        ('single-000014', '20250212005400110437', 25, '44000', FALSE),
        ('single-000015', '20250219005400210461', 25, '43000', FALSE),
        ('single-000016', '20250220005400210493', 25, '43000', FALSE),
        ('single-000017', '20250221005400210534', 25, '27000', FALSE),
        ('single-000018', '20250316005400210627', 25, '11000', FALSE),
        ('single-000019', '20250513005400210810', 25, '12000', FALSE),
        ('single-000023', '20250618005400111024', 25, '27000', FALSE),
        ('single-000024', '20250619005400111065', 25, '28000', FALSE),
        ('single-000025', '20250620005400111087', 20, '30000', FALSE),
        ('single-000027', '20250624005400111127', 25, '36000', FALSE),
        ('single-000029', '20250707005400211155', 35, '31000', FALSE),
        ('single-000030', '20250710005400211174', 28, '12000', FALSE),
        ('single-000031', '20250714005400211207', 25, '31000', FALSE),
        ('single-000033', '20250715005400111249', 25, '31000', FALSE),
        ('single-000034', '20250715005400111252', 25, '31000', FALSE),
        ('single-000036', '20250715005400211271', 25, '44000', FALSE),
        ('single-000037', '20250716005400111300', 19, '31000', FALSE),
        ('single-000038', '20250716005400211319', 25, '52000', FALSE),
        ('single-000039', '20250717005400211353', 25, '50000', FALSE),
        ('single-000040', '20250718005400211378', 25, '50000', FALSE),
        ('single-000041', '20250718005400211385', 32, '28000', FALSE),
        ('single-000042', '20250718005400211395', 25, '50000', FALSE),
        ('single-000043', '20250718005400211408', 25, '28000', FALSE),
        ('single-000044', '20250718005400211412', 25, '52000', FALSE),
        ('single-000045', '20250718005400211414', 25, '28000', FALSE),
        ('single-000050', '20250904005400211647', 25, '36000', FALSE),
        ('single-000051', '20250917005400211708', 25, '48000', FALSE),
        ('single-000052', '20250917005400211727', 35, '48000', FALSE),
        ('single-000053', '20250917005400211728', 25, '48000', FALSE),
        ('single-000054', '20250917005400211732', 25, '48000', FALSE),
        ('single-000055', '20250918005400211763', 25, '48000', FALSE),
        ('single-000056', '20250922005400211788', 35, '48000', FALSE),
        ('single-000057', '20250924005400211793', 25, '48000', FALSE),
        ('single-000058', '20251107005400211818', 25, '50000', FALSE),
        ('single-000060', '20251214005400211990', 25, '44000', FALSE),
        ('single-000062', '20251214005400212007', 25, '44000', FALSE),
        ('single-000063', '20251218005400212031', 25, '48000', FALSE),
        ('single-000066', '20260310005400112102', 25, '27000', FALSE),
        ('single-000067', '20260311005400112111', 21, '28000', FALSE),
        ('single-000068', '20260313005400212146', 25, '41000', FALSE),
        ('single-000069', '20260317005400112169', 25, '31000', FALSE),
        ('single-000070', '20260318005400212193', 25, '41000', TRUE),
        ('single-000071', '20260319005400212222', 25, '12000', FALSE),
        ('single-000072', '20260325005400212270', 25, '47000', FALSE),
        ('single-000074', '20260402005400212379', 28, '31000', FALSE),
        ('single-000075', '20260402005400212386', 25, '31000', FALSE),
        ('single-000076', '20260406005400112497', 25, '48000', FALSE),
        ('single-000077', '20260406005400212458', 25, '28000', FALSE),
        ('single-000078', '20260406005400212476', 25, '28000', FALSE),
        ('single-000080', '20260406005400212490', 25, '28000', FALSE),
        ('single-000082', '20260406005400212514', 28, '28000', FALSE),
        ('single-000084', '20260406005400212528', 25, '31000', FALSE),
        ('single-000086', '20260407005400212542', 25, '31000', FALSE),
        ('single-000088', '20260408005400212594', 25, '12000', FALSE),
        ('single-000089', '20260408005400212599', 25, '12000', FALSE),
        ('single-000090', '20260408005400212615', 25, '12000', FALSE),
        ('single-000091', '20260410005400212670', 25, '31000', FALSE),
        ('single-000092', '20260413005400212704', 25, '31000', FALSE),
        ('single-000093', '20260413005400212711', 25, '31000', TRUE),
        ('single-000095', '20260413005400212721', 25, '50000', FALSE),
        ('single-000096', '20260417005400112768', 25, '44000', FALSE),
        ('single-000098', '20260422005400212846', 35, '50000', FALSE),
        ('single-000099', '20260427005400212884', 25, '48000', FALSE),
        ('single-000100', '20260429005400212904', 25, '26000', FALSE),
        ('single-000101', '20260429005400212944', 25, '41000', FALSE),
        ('single-000102', '20260429005400212947', 25, '41000', FALSE),
        ('single-000104', '20260430005400212987', 25, '44000', FALSE),
        ('single-000105', '20260430005400212993', 25, '44000', FALSE),
        ('single-000106', '20260430005400212998', 25, '44000', FALSE),
        ('single-000107', '20260430005400213000', 25, '44000', TRUE),
        ('single-000108', '20260430005400213005', 25, '44000', FALSE),
        ('single-000109', '20260504005400113074', 25, '44000', FALSE),
        ('single-000111', '20260504005400213052', 25, '44000', FALSE),
        ('single-000115', '20260527005400113224', 25, '41000', FALSE),
        ('single-000117', '20260722005400213295', 28, '12000', FALSE),
        ('single-000118', '20260724005400213300', 25, '28000', FALSE),
        ('single-000119', '20260728005400213309', 25, '12000', FALSE),
        ('single-000123', '20260918005400113441', 25, '50000', FALSE),
        ('single-000124', '20260921005400113468', 25, '51000', FALSE),
        ('single-000125', '20260921005400113469', 25, '52000', FALSE),
        ('single-000126', '20260921005400113483', 25, '11000', FALSE),
        ('single-000127', '20260922005400113532', 25, '12000', TRUE),
        ('single-000128', '20260923005400113565', 25, '26000', FALSE),
        ('single-000129', '20260923005400113580', 25, '27000', FALSE),
        ('single-000073', '20260325005400212275', 25, '51000', FALSE),
        ('single-000097', '20260421005400212800', 25, '50000', FALSE),
        ('single-000130', '20260504005400213025', 25, '44000', FALSE),
        ('single-000131', '20260318005400212198', 25, '41000', FALSE),
        ('single-000132', '20260504005400213030', 25, '44000', FALSE),
        ('single-000133', '20260930005400113738', 25, '27000', FALSE)
),
checked AS (
    SELECT
        g.sample_id,
        g.policy_no,
        p.policy_name,
        p.policy_no IS NOT NULL AS found,
        p.is_active,
        CASE
            WHEN p.policy_no IS NULL THEN NULL
            WHEN p.application_period_type_code = '0057002' THEN '상시'
            WHEN p.application_period_type_code = '0057003' THEN '마감'
            WHEN p.application_start_date IS NULL AND p.application_end_date IS NULL THEN '정보 없음'
            WHEN p.application_end_date < (now() AT TIME ZONE 'Asia/Seoul')::date THEN '마감'
            WHEN p.application_start_date > (now() AT TIME ZONE 'Asia/Seoul')::date THEN '예정'
            ELSE '진행 중'
        END AS status,
        g.open_only_question,
        g.age IS NULL
            OR ((p.min_age IS NULL OR p.min_age <= g.age) AND (p.max_age IS NULL OR p.max_age >= g.age)) AS age_ok,
        g.region_code IS NULL OR EXISTS (
            SELECT 1 FROM v_policy_region_resolved v
            WHERE v.policy_no = p.policy_no AND v.sido_code = left(g.region_code, 2)
        ) AS region_ok,
        (SELECT count(*) FROM policy_chunk c
         WHERE c.policy_no = p.policy_no
           AND c.embedding IS NOT NULL
           AND c.embedding_model = 'bedrock-titan-embed-v2') AS embedded_chunks,
        (SELECT string_agg(o.policy_no, ', ' ORDER BY o.policy_no) FROM policy o
         WHERE o.is_active AND o.policy_name = p.policy_name AND o.policy_no <> p.policy_no) AS same_name_policies,
        g.age AS profile_age,
        p.min_age,
        p.max_age,
        g.region_code AS profile_region
    FROM golden g
    LEFT JOIN policy p ON p.policy_no = g.policy_no
),
judged AS (
    SELECT
        CASE
            WHEN NOT found OR NOT is_active THEN '1 제외'
            WHEN embedded_chunks = 0 THEN '2 적재 확인'
            WHEN NOT age_ok OR NOT region_ok THEN '3 프로필 재생성'
            WHEN status = '마감' AND open_only_question THEN '4 질문 확인'
            WHEN status IN ('마감', '예정', '정보 없음') OR same_name_policies IS NOT NULL THEN '5 참고'
            ELSE '6 정상'
        END AS verdict,
        concat_ws(', ',
            CASE WHEN NOT found THEN 'DB에 없음' END,
            CASE WHEN found AND NOT is_active THEN '비활성' END,
            CASE WHEN found AND embedded_chunks = 0 THEN '임베딩 청크 없음' END,
            CASE WHEN found AND NOT age_ok THEN '나이 불일치' END,
            CASE WHEN found AND NOT region_ok THEN '지역 불일치' END,
            CASE WHEN status = '마감' AND open_only_question THEN '마감인데 지금 신청 질문' END,
            CASE WHEN status IN ('마감', '예정', '정보 없음') THEN status || ' 감점' END,
            CASE WHEN same_name_policies IS NOT NULL THEN '같은 이름 공고 있음' END
        ) AS issues,
        checked.*
    FROM checked
)
SELECT
    verdict AS "판정",
    count(*) OVER (PARTITION BY verdict) AS "판정 건수",
    sample_id,
    policy_no,
    policy_name,
    status AS "신청 상태",
    issues AS "문제",
    embedded_chunks AS "임베딩 청크",
    profile_age AS "프로필 나이",
    min_age,
    max_age,
    profile_region AS "프로필 지역",
    same_name_policies AS "같은 이름 공고"
FROM judged
ORDER BY verdict, sample_id;
-- 기대 결과: 100행. "1 제외"~"4 질문 확인"이 0건이면 golden_v3로 바로 평가한다
