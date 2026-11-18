# Two images, each for a task of its own: `bot` (the relay and the sample bot) and `match` (the
# controller and the match task's entrypoint). The build context is platform/; build and push them
# with prototype/push-image.sh.
#
# The match image takes its controller binary from another image: CONTROLLER_IMAGE, at
# CONTROLLER_PATH. By default that's this repository's mock controller (platform/Dockerfile,
# target `controller`), which plays the toy games; any game's own controller image works the same.
ARG CONTROLLER_IMAGE=dsim/controller:local

FROM ${CONTROLLER_IMAGE} AS controller

FROM rust:1.99.0-alpine3.23@sha256:42c2519fdf75d9e34cc61a1aaf34b1a684da9bfd65fe5daefef98d92a801b001 AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p relay \
 && mkdir -p /dist \
 && cp target/release/relay /dist/

FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS bot
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot
COPY --from=build /dist/relay /app/relay
COPY bots/python/bot.py /app/bot.py
COPY prototype/task/isolation_probe.py /app/isolation_probe.py
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001
# The deadline is the longest a match can take (connect_ms + total_ms) plus 5 minutes. As PID 1 it
# outlives anything the bot does, and the task ends with it even if the controller vanished.
ENTRYPOINT ["timeout", "--kill-after=5", "4200"]
CMD ["/app/relay", "--", "python3", "/app/bot.py"]

FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS match
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin match
COPY prototype/task/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-compile -r /app/requirements.txt
ARG CONTROLLER_PATH=/mock-controller
COPY --from=controller ${CONTROLLER_PATH} /app/controller
COPY prototype/task/match_task.py prototype/task/bot_tasks.py /app/
ARG IMAGE_REVISION=unknown
# The controller image's GAME_VERSION, and how match_task.py reads its scores (push-image.sh).
ARG GAME_VERSION=unknown
ARG SCORE_FORMAT=plain
ENV IMAGE_REVISION=${IMAGE_REVISION} GAME_VERSION=${GAME_VERSION} SCORE_FORMAT=${SCORE_FORMAT} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
ENTRYPOINT ["python3", "/app/match_task.py"]
CMD ["play"]
