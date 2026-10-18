FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY seismo ./seismo
COPY static ./static
COPY data/sample.jsonl ./data/sample.jsonl
ENV PORT=8080
CMD ["sh", "-c", "uvicorn seismo.server:app --host 0.0.0.0 --port ${PORT}"]
