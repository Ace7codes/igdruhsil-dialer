FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (.env is NOT copied — it's mounted at runtime; see .dockerignore)
COPY app.py .
COPY static/ static/

EXPOSE 3001

# 1 worker so the in-memory call/message log is consistent; threads for concurrency.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:3001", "app:app"]
