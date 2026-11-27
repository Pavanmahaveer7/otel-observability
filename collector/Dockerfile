# syntax=docker/dockerfile:1
# Builds the custom collector distribution from builder-config.yaml with OCB.
#   docker build -t obsdemo/otelcol-obs:0.1.0 collector
#   docker build --build-arg MANIFEST=builder-config.cloud.yaml -t obsdemo/otelcol-obs:0.1.0-full collector
FROM golang:1.27 AS build
ARG OCB_VERSION=0.166.0
ARG MANIFEST=builder-config.yaml
RUN --mount=type=cache,target=/root/go/pkg/mod \
    go install go.opentelemetry.io/collector/cmd/builder@v${OCB_VERSION}
WORKDIR /build
COPY ${MANIFEST} builder-config.yaml
RUN --mount=type=cache,target=/root/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 builder --config builder-config.yaml

# Runs as root by default: the node agent reads /var/log/pods and host /proc.
# The gateway can run unprivileged (see securityContext in the chart).
FROM gcr.io/distroless/static-debian12
COPY --from=build /build/_build/otelcol-obs /otelcol-obs
EXPOSE 4317 4318 8888 8889 13133
ENTRYPOINT ["/otelcol-obs"]
CMD ["--config=/conf/config.yaml"]
