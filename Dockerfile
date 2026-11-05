FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_FILE=/data/state.json

# tzdata: TZ 환경변수가 로그 시각과 스케줄 모두에 적용되도록
RUN apk add --no-cache tzdata \
 && adduser -D -u 1000 app && mkdir /data && chown app:app /data
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ .

USER app
VOLUME ["/data"]

CMD ["python", "monitor.py"]
