FROM python:3.11-slim

WORKDIR /app

COPY global-events-producer/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY shared/ ./shared/
COPY global-events-producer/ ./global-events-producer/

ENV PYTHONPATH=/app

CMD ["python", "global-events-producer/main.py"]
