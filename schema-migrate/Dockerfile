FROM python:3.11-slim

WORKDIR /app

COPY schema-migrate/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY schema-migrate/ ./schema-migrate/

ENV PYTHONUNBUFFERED=1

CMD ["python", "schema-migrate/migrate.py"]
