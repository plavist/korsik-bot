FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/srv/.venv/bin:$PATH"
# Корневой/промежуточные УЦ Минцифры — нужны только для MAX API (platform-api2.max.ru)
COPY certs/russian_trusted_ca.pem /certs/russian_trusted_ca.pem
ENV MAX_CA_BUNDLE=/certs/russian_trusted_ca.pem
COPY app app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
