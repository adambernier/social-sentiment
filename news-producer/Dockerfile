FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY news-producer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared code and service code
COPY shared/ ./shared/
COPY news-producer/ ./news-producer/

ENV PYTHONUNBUFFERED=1

CMD ["python", "news-producer/main.py"]
