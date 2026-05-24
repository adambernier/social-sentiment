FROM python:3.11-slim
WORKDIR /app
COPY reddit-producer/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY shared/ ./shared/
COPY reddit-producer/ ./reddit-producer/
ENV PYTHONPATH=/app
CMD ["python", "reddit-producer/main.py"]
