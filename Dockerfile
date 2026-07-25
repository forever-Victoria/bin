FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8767 \
    HOME=/tmp

WORKDIR /app

COPY . .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN addgroup --system gateway \
    && adduser --system --ingroup gateway gateway \
    && chown -R gateway:gateway /app

USER gateway

EXPOSE 8767

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8767') + '/healthz', timeout=3)"

CMD ["python", "main.py"]
