# Only needed with ENABLE_PERF=true.
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends linux-perf \
 && rm -rf /var/lib/apt/lists/*
