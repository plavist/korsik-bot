FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/srv/.venv/bin:$PATH"
COPY app app
# не root: процесс сервиса не должен иметь лишних прав в контейнере
RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /data && chown app /data
USER app
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
  CMD python -c "import os, socket; socket.create_connection(('127.0.0.1', int(os.getenv('HEALTH_PORT', '8000'))), 3).close()"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
