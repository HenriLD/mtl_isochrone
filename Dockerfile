# Hugging Face Docker Space image. PyPy (not CPython) for the JIT speedup on the
# pure-Python RAPTOR engine; all server deps are pure-Python so nothing compiles.
FROM pypy:3.10-slim

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code (data pickles are NOT copied — see .dockerignore; they're fetched at
# boot from the HF dataset configured by HF_DATA_REPO, or mounted via storage)
COPY . .

ENV PYTHONUNBUFFERED=1
# HF Spaces give the container a writable HOME at /data only with persistent
# storage; default the hub cache somewhere always-writable.
ENV HF_HOME=/tmp/hf

# HF Spaces route to port 7860 by default (see app_port in README.md)
EXPOSE 7860
CMD ["pypy3", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "7860"]
