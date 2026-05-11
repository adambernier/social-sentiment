FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY market-producer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared code and service code
# We need storage_service too because we import DB from it
COPY shared/ ./shared/
COPY storage-service/ ./storage_service/
COPY market-producer/ ./market-producer/

ENV PYTHONUNBUFFERED=1

CMD ["python", "market-producer/main.py"]
