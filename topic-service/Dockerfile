FROM python:3.11-slim

WORKDIR /app

# Install build tools for hdbscan/umap
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY topic-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY topic-service/ ./topic-service/

# Set Python path to find shared module
ENV PYTHONPATH=/app

# HF_HOME is used for caching sentence-transformers
ENV HF_HOME=/app/.cache/huggingface

CMD ["python", "topic-service/main.py"]
