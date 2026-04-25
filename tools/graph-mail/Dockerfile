FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -e .

RUN mkdir -p /app/artifacts

CMD ["python", "src/main.py", "/input.json"]
