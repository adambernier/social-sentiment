FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY ui-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY shared/ ./shared/
COPY ui-service/ ./ui-service/

EXPOSE 8501

CMD ["streamlit", "run", "ui-service/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.enableCORS=false"]
