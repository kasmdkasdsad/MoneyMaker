FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "pydantic>=2.7" "pydantic-settings>=2.2" \
      "sqlalchemy>=2.0" "psycopg[binary]>=3.1" "uvicorn[standard]>=0.30"

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
