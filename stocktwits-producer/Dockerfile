FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY stocktwits-producer/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy shared code and service code
COPY shared/ ./shared/
COPY stocktwits-producer/ ./stocktwits-producer/

ENV PYTHONUNBUFFERED=1

CMD ["python", "stocktwits-producer/main.py"]
