FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MADI_BIND=0.0.0.0
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home madi
COPY server.py inspector.py ./
COPY engine ./engine
COPY static ./static
USER madi
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8765), 3).close()"
CMD ["python", "server.py", "8765"]
