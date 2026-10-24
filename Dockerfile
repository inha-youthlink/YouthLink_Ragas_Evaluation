# 평가 CLI(python -m ragas_eval)를 K8s Job으로 실행하기 위한 이미지
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ragas_eval/ ragas_eval/
COPY datasets/ datasets/

RUN useradd --system --uid 10001 --no-create-home ragas \
    && mkdir -p results \
    && chown ragas:ragas results
USER 10001

# 비밀값(OPENAI_API_KEY, DATABASE_URL)은 이미지에 넣지 않고 실행 시 환경변수로 주입한다.
ENTRYPOINT ["python", "-m", "ragas_eval"]
CMD ["--help"]
