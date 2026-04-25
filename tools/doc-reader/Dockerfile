FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -e .

RUN mkdir -p /app/artifacts

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import sys; sys.exit(0)"

CMD ["python3", "src/main.py", "/input.json"]
