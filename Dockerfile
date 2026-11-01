FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --create-home app
COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests
COPY docs ./docs
COPY web/dist ./web/dist
USER app
CMD ["uvicorn", "astraroll.api:app", "--host", "0.0.0.0", "--port", "8000"]
