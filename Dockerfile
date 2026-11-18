FROM denoland/deno:bin-2.9.8 AS deno
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    QUIRE_YOUTUBE_TOOLS=/usr/local/bin ONDIER_DATA=/data QUIRE_PODCAST_DATA=/data
COPY --from=deno /deno /usr/local/bin/deno
RUN pip install --no-cache-dir 'yt-dlp[default]==2026.8.19' && \
    yt-dlp --version && deno --version
WORKDIR /app
COPY app/ ./
COPY LICENSE ./LICENSE
COPY licenses/ ./licenses/
EXPOSE 8788
HEALTHCHECK --interval=10s --timeout=4s --start-period=3s CMD python -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:8788/health", timeout=3).close()'
CMD ["python", "server.py"]
