# syntax=docker/dockerfile:1

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir torch==2.11.0 \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY config.py cache.py progress.py convert.py sources.py server.py cover.py \
     render.py layout.py links.py extract.py assemble.py \
     junk_patterns.txt \
     ./
COPY web ./web

RUN mkdir -p books data output

ENV HF_HOME=/models
RUN mkdir -p /models

EXPOSE 8765

ENTRYPOINT ["python", "convert.py"]
CMD ["books/"]
