-- RAGAS 평가 실행과 샘플별 채점 결과를 저장하는 테이블
--
-- Run youthlink-data-pipeline/db/extensions.sql before this file (gen_random_uuid).

BEGIN;

CREATE TABLE IF NOT EXISTS ragas_evaluation_run (
    run_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_version         VARCHAR(50) NOT NULL,
    repeat_no               INTEGER NOT NULL DEFAULT 1,
    mode                    VARCHAR(20) NOT NULL DEFAULT 'rag',

    rag_chat_model          VARCHAR(100),
    rag_retriever           VARCHAR(50),
    rag_top_k               INTEGER,

    judge_model             VARCHAR(100) NOT NULL,
    embedding_model         VARCHAR(100) NOT NULL,

    status                  VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    sample_count            INTEGER,

    avg_context_precision   NUMERIC(4, 3),
    avg_context_recall      NUMERIC(4, 3),
    avg_faithfulness        NUMERIC(4, 3),
    avg_answer_relevancy    NUMERIC(4, 3),
    avg_factual_correctness     NUMERIC(4, 3),
    avg_reference_faithfulness  NUMERIC(4, 3),

    started_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at             TIMESTAMPTZ,

    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT chk_ragas_run_repeat_no
        CHECK (repeat_no >= 1),

    CONSTRAINT chk_ragas_run_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),

    CONSTRAINT chk_ragas_run_mode
        CHECK (mode IN ('rag', 'baseline', 'offline'))
);

CREATE INDEX IF NOT EXISTS idx_ragas_run_started
    ON ragas_evaluation_run (started_at);

CREATE INDEX IF NOT EXISTS idx_ragas_run_mode
    ON ragas_evaluation_run (mode, dataset_version);

CREATE TABLE IF NOT EXISTS ragas_evaluation_samples (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              UUID NOT NULL,
    sample_id           VARCHAR(50) NOT NULL,
    policy_no           VARCHAR(30),

    question            TEXT NOT NULL,
    answer              TEXT,
    ground_truth        TEXT,
    contexts            JSONB NOT NULL DEFAULT '[]'::jsonb,

    context_precision   NUMERIC(4, 3),
    context_recall      NUMERIC(4, 3),
    faithfulness        NUMERIC(4, 3),
    answer_relevancy    NUMERIC(4, 3),
    factual_correctness     NUMERIC(4, 3),
    reference_faithfulness  NUMERIC(4, 3),

    latency_ms          INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT fk_ragas_samples_run
        FOREIGN KEY (run_id)
        REFERENCES ragas_evaluation_run (run_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_ragas_samples_run_sample
        UNIQUE (run_id, sample_id),

    CONSTRAINT chk_ragas_samples_context_precision
        CHECK (context_precision IS NULL OR context_precision BETWEEN 0 AND 1),

    CONSTRAINT chk_ragas_samples_context_recall
        CHECK (context_recall IS NULL OR context_recall BETWEEN 0 AND 1),

    CONSTRAINT chk_ragas_samples_faithfulness
        CHECK (faithfulness IS NULL OR faithfulness BETWEEN 0 AND 1),

    CONSTRAINT chk_ragas_samples_answer_relevancy
        CHECK (answer_relevancy IS NULL OR answer_relevancy BETWEEN 0 AND 1),

    CONSTRAINT chk_ragas_samples_factual_correctness
        CHECK (factual_correctness IS NULL OR factual_correctness BETWEEN 0 AND 1),

    CONSTRAINT chk_ragas_samples_reference_faithfulness
        CHECK (reference_faithfulness IS NULL OR reference_faithfulness BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS idx_ragas_samples_run
    ON ragas_evaluation_samples (run_id);

CREATE INDEX IF NOT EXISTS idx_ragas_samples_policy
    ON ragas_evaluation_samples (policy_no);

-- answer_coverage: 이미 만들어진 테이블에도 추가되도록 ALTER로 둔다. 기존 행은 NULL
ALTER TABLE ragas_evaluation_run
    ADD COLUMN IF NOT EXISTS avg_answer_coverage NUMERIC(4, 3);

ALTER TABLE ragas_evaluation_samples
    ADD COLUMN IF NOT EXISTS answer_coverage NUMERIC(4, 3)
        CONSTRAINT chk_ragas_samples_answer_coverage
        CHECK (answer_coverage IS NULL OR answer_coverage BETWEEN 0 AND 1);

COMMIT;
