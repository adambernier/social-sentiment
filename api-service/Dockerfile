FROM python:3.11-slim

WORKDIR /app

# Copy the compiled, hashed lockfile and install with hash verification.
# Regenerate it from api-service/requirements.in with uv (see that file's header).
COPY api-service/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy shared and service code
COPY shared/ ./shared/
COPY api-service/ ./api-service/

# Set Python path to find shared module
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["python", "api-service/main.py"]
