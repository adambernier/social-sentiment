FROM python:3.11-slim

WORKDIR /app

COPY archive-service/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY archive-service/ ./archive-service/

ENV PYTHONUNBUFFERED=1

CMD ["python", "archive-service/export.py"]
