FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

EXPOSE 8000

# Bootstrap applies db/schema.sql + functions + seed (idempotent), then serve.
CMD ["sh", "-c", "python -m ledgerforge.bootstrap && uvicorn ledgerforge.main:app --host 0.0.0.0 --port 8000"]
