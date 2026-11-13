# The hub: one static Go binary, nothing else in the image.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -X main.Version=${VERSION}" -o /hub .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /hub /hub
# The Docker socket is the hub's to use: it runs as root inside its container.
VOLUME /data
EXPOSE 35100
ENTRYPOINT ["/hub"]
