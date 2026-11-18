# Images for local matches. Base images are pinned by digest and bumped by hand.
#   docker build --target controller -t dsim/controller .
#   docker build --target bot-python -t dsim/bot-python .
# The controller stage is the reference for how a game should build its controller image.

FROM rust:1.99.0-alpine3.23@sha256:42c2519fdf75d9e34cc61a1aaf34b1a684da9bfd65fe5daefef98d92a801b001 AS build
WORKDIR /src
COPY . .
# Static musl binaries: they run on distroless/static and inside any bot image.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked -p mock-controller -p relay \
 && mkdir -p /dist/out \
 && cp target/release/mock-controller target/release/relay /dist/

# The game controller: no shell, no package manager, runs as uid 65532.
FROM gcr.io/distroless/static-debian13:nonroot@sha256:d62067879de890a1def9da0e7766b7d20810a5e5555cef8506b752437d7a3e88 AS controller
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
COPY --from=build /dist/mock-controller /mock-controller
COPY --from=build --chown=65532:65532 /dist/out /out
ENV PORT=9000 OUT_DIR=/out GAME_VERSION=mock
EXPOSE 9000
ENTRYPOINT ["/mock-controller"]

# A Python bot behind the relay, built the way submission builds will build bots.
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS bot-python
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot
COPY --from=build /dist/relay /relay
COPY bots/python/bot.py /bot/bot.py
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001
ENTRYPOINT ["/relay", "--", "python3", "/bot/bot.py"]
