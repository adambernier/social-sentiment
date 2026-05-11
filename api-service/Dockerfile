FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY api-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY api-service/ ./api-service/

# Set Python path to find shared module
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python", "api-service/main.py"]
