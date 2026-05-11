FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY sentiment-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY sentiment-service/ ./sentiment-service/

# Set Python path to find shared module
ENV PYTHONPATH=/app

# HF_HOME is used for caching models
ENV HF_HOME=/app/.cache/huggingface

CMD ["python", "sentiment-service/main.py"]
