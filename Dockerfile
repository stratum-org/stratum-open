# The GitHub App mode: one container on a server the maintainers run.
# Keep app.pem outside the image and readable by the container user.
#   docker build -t stratum-open .
#   docker run -p 8080:8080 -v stratum-data:/data \
#     --mount "type=bind,src=$PWD/app.pem,dst=/run/secrets/app.pem,readonly" \
#     -e STRATUM_APP_ID=... -e STRATUM_WEBHOOK_SECRET=... -e STRATUM_APP_PRIVATE_KEY_FILE=/run/secrets/app.pem \
#     -e STRATUM_MODEL_ENDPOINT=http://your-ollama:11434/v1 stratum-open
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home stratum
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY stratum_open ./stratum_open
RUN pip install --no-cache-dir ".[app]" && mkdir /data && chown stratum /data

USER stratum
ENV STRATUM_DB=/data/stratum.db STRATUM_WORKDIR=/data/repos PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["stratum-open", "serve"]
