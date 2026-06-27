# Python 3.11 on a slim Debian base
FROM python:3.11-slim

# ffmpeg is required by yt-dlp for merging video streams and for MP3 conversion.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Keep Python output unbuffered so logs show up immediately in Render.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the application code.
COPY . .

# Render provides $PORT at runtime; default to 10000 for local docker runs.
ENV PORT=10000
EXPOSE 10000

# Bind to 0.0.0.0 and the Render-provided PORT. Shell form is required so that
# ${PORT} is expanded at container start. A single worker + a small concurrency
# limit keeps memory bounded on tiny instances (e.g. Render Free 512 MB).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --limit-concurrency 4"]
