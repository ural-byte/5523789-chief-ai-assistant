FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
RUN useradd --uid 10001 --create-home assistant && mkdir -p /srv/data/documents && chown -R assistant:assistant /srv/data
USER assistant
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
