FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir . \
    && chmod +x /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
