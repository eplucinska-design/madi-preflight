FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MADI_BIND=0.0.0.0 PORT=8765
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home madi
COPY server.py inspector.py ./
COPY engine ./engine
COPY static ./static
USER madi
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8765'), timeout=3).close()"
# Port comes from PORT (set by most container hosts). Access mode: MADI_PASSWORD or MADI_PUBLIC=1.
CMD ["python", "server.py"]
