FROM mcr.microsoft.com/playwright/python:v1.59.1-noble

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -e .

RUN mkdir -p /app/artifacts

CMD ["python", "src/main.py", "/input.json"]
