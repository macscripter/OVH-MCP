# ======================================================================================
# simpl-ovh-mcp — MCP server for deploying Simpl-Open on OVHcloud
#
# Two things go into the image beyond the Python package: the helm binary, because the
# four platform charts are installed with it, and a vendored copy of the Bridge chart, so
# the Bridge can be deployed without a checkout of its repository.
#
# Everything Simpl-Open publishes is deployed through the Kubernetes API as ArgoCD
# Applications, so kubectl is deliberately absent.
# ======================================================================================
FROM python:3.12-slim AS base

# Helm 3.19: the programme's prerequisites ask for 3.14 or higher. Helm 4 exists and is not
# used here — the charts in question have been verified against the 3.x line, and a
# deployment tool is the wrong place to find out that a major version changed rendering.
ARG HELM_VERSION=v3.19.0
ARG HELM_SHA256_AMD64=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496
ARG HELM_SHA256_ARM64=440cf7add0aee27ebc93fada965523c1dc2e0ab340d4348da2215737fc0d76ad
ARG TARGETARCH=amd64

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH}" in \
      amd64) HELM_SHA="${HELM_SHA256_AMD64}" ;; \
      arm64) HELM_SHA="${HELM_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    echo "${HELM_SHA}  /tmp/helm.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tar.gz -C /tmp; \
    install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tar.gz "/tmp/linux-${TARGETARCH}"; \
    apt-get purge -y curl; apt-get autoremove -y; \
    helm version --short

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY vendor ./vendor
RUN pip install --no-cache-dir .

# The state directory holds deployment profiles, kubeconfigs and the audit log. Mount a
# Railway volume at /data to keep them across deploys; without one the server still runs,
# and forgets its profiles on every restart.
ENV SIMPL_MCP_STATE_DIR=/data \
    SIMPL_MCP_TRANSPORT=http \
    SIMPL_MCP_HOST=0.0.0.0 \
    BRIDGE_CHART_PATH=/app/vendor/charts/bridge \
    PYTHONUNBUFFERED=1

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && mkdir -p /data && chown -R mcp:0 /data /app

# The container starts as root and the entrypoint drops to uid 10001 after taking
# ownership of the mounted volume. Declaring USER here instead would leave the volume
# root-owned and unwritable — the platform mounts it after the image is built.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "simpl_ovh_mcp"]
