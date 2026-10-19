# ════════════════════════════════════════════════════════════════════════════
# ReferralEasy — OTel Collector image (thin wrapper)
#
# WHY THIS EXISTS
# The upstream image otel/opentelemetry-collector-contrib:0.163.1 is built
# FROM scratch: verified in this repo's environment, it has no shell and no
# HTTP client (`docker run --entrypoint sh ...` -> "sh: executable file not
# found in $PATH"). A container-level healthcheck therefore cannot be
# expressed against it, and this stack requires healthchecks that are REAL
# probes rather than `CMD true`.
#
# This image adds exactly one thing: a busybox userland (alpine) so
# `wget` can probe the collector's own health_check extension on :13133.
# The collector binary is copied byte-for-byte from the pinned upstream
# image — no collector code is rebuilt and no extra runtime is added.
#
# The collector config is bind-mounted by compose.yaml rather than baked in,
# so editing infra/otel/otel-collector-config.yaml only needs a container
# restart, not an image rebuild.
# ════════════════════════════════════════════════════════════════════════════
FROM alpine:3.24

RUN apk add --no-cache ca-certificates \
    && adduser -D -u 10001 otel

COPY --from=otel/opentelemetry-collector-contrib:0.163.1 /otelcol-contrib /otelcol-contrib

USER 10001:10001

ENTRYPOINT ["/otelcol-contrib"]
CMD ["--config=/etc/otelcol-contrib/config.yaml"]
