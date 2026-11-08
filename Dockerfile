FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg python3 python3-venv ca-certificates fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/victor-venv
ENV PATH="/opt/victor-venv/bin:${PATH}"
RUN pip install --no-cache-dir "yt-dlp[default]" "faster-whisper>=1.1,<2"
WORKDIR /app
COPY server.js clip_engine.py ./
COPY public/ ./public/
ENV PORT=3000 WHISPER_MODEL=base
EXPOSE 3000
CMD ["node","server.js"]
