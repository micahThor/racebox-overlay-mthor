# The club web app (racebox-overlay serve). Built and pushed to ECR by
# .github/workflows/deploy.yml; see docs/aws.md.
FROM python:3.12-slim-bookworm

# ffmpeg renders the video; the fonts fill in for the Mac ones (the app brings its own too)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-liberation2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY rbo ./rbo
RUN pip install --no-cache-dir ".[server]" \
 && useradd --uid 1000 --create-home rbo \
 && mkdir -p /data && chown rbo /data

USER rbo
ENV RBO_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["racebox-overlay", "serve", "--host", "0.0.0.0", "--port", "8080"]
