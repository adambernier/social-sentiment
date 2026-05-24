FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY preprocessing-service/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY preprocessing-service/ ./preprocessing-service/

# Set Python path to find shared module
ENV PYTHONPATH=/app

CMD ["python", "preprocessing-service/main.py"]
