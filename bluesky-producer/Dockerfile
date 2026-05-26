FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY bluesky-producer/requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY bluesky-producer/ ./bluesky-producer/

# Set Python path to find shared module
ENV PYTHONPATH=/app

CMD ["python", "bluesky-producer/main.py"]
