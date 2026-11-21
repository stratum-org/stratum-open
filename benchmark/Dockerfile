# What benchmark/build_checks.py builds the Go and Rust projects with, for the
# Build-check benchmark workflow: Go and staticcheck, Rust with Clippy, Python
# with this package. The workflow runs each project's builds in a container
# made from it, so the projects' code runs away from the runner. Each base
# image is pinned by digest. Build it from the repository's root:
#
#     docker build -f benchmark/Dockerfile -t stratum-build-checks .

FROM golang:1.27.4-trixie@sha256:f18c3d73a184fb0582828510722e723b63a93f7188d11dd223d9be6f6880e65d AS go

FROM rust:1.100.0-trixie@sha256:223cc32ddca8f3ee90c436dbc94b6b5f052ea7f9653c5201e9612f08ad7d21b8
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/root/go/bin:/usr/local/go/bin:$PATH
# staticcheck's release, then the caches cleared, so a project's first PR is built with nothing cached.
RUN rustup component add clippy \
 && go install honnef.co/go/tools/cmd/staticcheck@2026.2.2 \
 && go clean -cache -modcache \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --quiet --no-cache-dir /src
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
