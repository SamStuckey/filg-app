# FILG MVP — host-agnostic container (Render / Fly / Railway / anything).
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Cost guardrail defaults — override ANTHROPIC_API_KEY + FILG_PAID_EMAILS as secrets at deploy.
ENV FILG_FREE_RUNS=1 FILG_DAILY_BUDGET=5
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
