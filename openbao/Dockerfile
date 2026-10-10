# OpenBao image with the oauthapp secrets plugin baked in.
# The plugin publishes no release binaries, so we build it from a pinned commit.

ARG OPENBAO_VERSION=2.7.1

FROM golang:1.26.9-alpine AS plugin
ARG PLUGIN_REF=171c340511a1d8a67b3544cdadc4f036c0e9bc79
RUN apk add --no-cache git
WORKDIR /src
RUN git init -q . \
 && git remote add origin https://github.com/openbao/openbao-plugin-secrets-oauthapp \
 && git fetch -q --depth 1 origin "${PLUGIN_REF}" \
 && git checkout -q FETCH_HEAD
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
      -o /out/oauthapp ./cmd/openbao-plugin-secrets-oauthapp \
 && sha256sum /out/oauthapp | cut -d' ' -f1 > /out/oauthapp.sha256

FROM openbao/openbao:${OPENBAO_VERSION}
COPY --from=plugin --chown=openbao:openbao /out/oauthapp /openbao/plugins/oauthapp
# Checksum lets the bootstrap script register the plugin. It lives outside the
# plugin dir because OpenBao treats every file there as a plugin binary.
COPY --from=plugin --chown=openbao:openbao /out/oauthapp.sha256 /openbao/oauthapp.sha256
