FROM debian:bookworm-slim AS wal-g

ARG WALG_VERSION=v3.0.8
ARG TARGETARCH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && case "${TARGETARCH}" in \
        amd64) \
            asset="wal-g-pg-24.04-amd64"; \
            checksum="342574292b1907af738d48ff2d1d771ad90a63e441b40a85208022144253f6b8" \
            ;; \
        arm64) \
            asset="wal-g-pg-24.04-aarch64"; \
            checksum="a822caafa9ee61c2f96add3e768c06971677d8b7a6781e585253b8735a3bc4f7" \
            ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --show-error --silent --location \
        "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${asset}" \
        --output /wal-g \
    && echo "${checksum}  /wal-g" | sha256sum --check --strict \
    && chmod 0755 /wal-g

FROM postgres:15

COPY --from=wal-g /wal-g /usr/local/bin/wal-g
COPY postgres/docker-entrypoint-walg.sh /usr/local/bin/docker-entrypoint-walg.sh
COPY postgres/restore-drill.sh /usr/local/bin/restore-drill.sh

RUN chmod 0755 \
    /usr/local/bin/docker-entrypoint-walg.sh \
    /usr/local/bin/restore-drill.sh

ENTRYPOINT ["docker-entrypoint-walg.sh"]
CMD ["postgres"]
