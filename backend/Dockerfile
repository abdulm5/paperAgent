# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS production

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 pageragent \
    && useradd --uid 10001 --gid pageragent --no-create-home --shell /usr/sbin/nologin pageragent

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY --from=runbooks . /runbooks
COPY --from=scenarios . /scenarios

RUN chown -R pageragent:pageragent /app /runbooks /scenarios

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/api/v1/health/live',headers={'Host':os.getenv('PAGERAGENT_HEALTH_HOST','127.0.0.1')}); os.getenv('SERVICE_NAME','pageragent-api')!='pageragent-api' or urllib.request.urlopen(r,timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM production AS development

USER root

COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY tests ./tests

USER 10001:10001
