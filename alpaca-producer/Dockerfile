FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY alpaca-producer/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy shared code and service code
COPY shared/ ./shared/
COPY alpaca-producer/ ./alpaca-producer/

ENV PYTHONUNBUFFERED=1

CMD ["python", "alpaca-producer/main.py"]
