FROM debian:trixie-20260824-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132 AS runner
RUN apt-get update \
    && apt-get install -y --no-install-recommends netcat-openbsd iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ./backend/backend /usr/local/bin/backend
COPY ./backend/lines ./lines
COPY ./entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 9400
RUN chmod +x /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/backend
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["backend"]
