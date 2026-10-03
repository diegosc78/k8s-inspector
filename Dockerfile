ARG UV_VERSION=0.12.22
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim

# TARGETARCH lo rellena buildx (amd64 | arm64)
ARG TARGETARCH

# Versiones fijadas (por defecto, pensadas para un clúster Kubernetes 1.36).
# kubectl admite ±1 versión menor respecto al API server.
ARG KUBECTL_VERSION=v1.36.5
ARG HELM_VERSION=v4.3.0
ARG K9S_VERSION=v0.51.0
ARG STERN_VERSION=v1.34.0
ARG YQ_VERSION=v4.54.1
ARG KUBECTX_VERSION=v0.11.0
ARG KREW_VERSION=v0.5.0
ARG POPEYE_VERSION=v0.22.1
ARG KUBE_SCORE_VERSION=v1.20.0
ARG KUBECONFORM_VERSION=v0.8.0
ARG TRIVY_VERSION=v0.75.0
# calicoctl debe coincidir con la versión de Calico del clúster (3.30.x)
ARG CALICOCTL_VERSION=v3.30.2
ARG CNPG_VERSION=v1.30.1
ARG KRR_REF=v1.30.0
ARG HOLMESGPT_VERSION=0.42.0
ARG NETSHOOT_IMAGE=nicolaka/netshoot:v0.14

# Herramientas base de depuración
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash-completion ca-certificates curl git jq less vim-tiny \
    dnsutils iputils-ping netcat-openbsd openssl skopeo bsdextrautils \
    && rm -rf /var/lib/apt/lists/*

# Binarios (versiones fijadas por ARG)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) XARCH=x86_64; TARCH=64bit ;; \
      arm64) XARCH=arm64;  TARCH=ARM64 ;; \
      *) echo "Arquitectura no soportada: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    gh() { echo "https://github.com/$1/releases/download/$2"; }; \
    cd /tmp; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl"; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" | tar xz --strip-components=1 -C /usr/local/bin "linux-${TARGETARCH}/helm"; \
    curl -fsSL "$(gh derailed/k9s ${K9S_VERSION})/k9s_Linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin k9s; \
    curl -fsSL "$(gh stern/stern ${STERN_VERSION})/stern_${STERN_VERSION#v}_linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin stern; \
    curl -fsSLo /usr/local/bin/yq "$(gh mikefarah/yq ${YQ_VERSION})/yq_linux_${TARGETARCH}"; \
    for t in kubectx kubens; do \
      curl -fsSL "$(gh ahmetb/kubectx ${KUBECTX_VERSION})/${t}_${KUBECTX_VERSION}_linux_${XARCH}.tar.gz" | tar xz -C /usr/local/bin "$t"; \
    done; \
    curl -fsSL "$(gh derailed/popeye ${POPEYE_VERSION})/popeye_linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin popeye; \
    curl -fsSL "$(gh zegl/kube-score ${KUBE_SCORE_VERSION})/kube-score_${KUBE_SCORE_VERSION#v}_linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin kube-score; \
    curl -fsSL "$(gh yannh/kubeconform ${KUBECONFORM_VERSION})/kubeconform-linux-${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin kubeconform; \
    curl -fsSL "$(gh aquasecurity/trivy ${TRIVY_VERSION})/trivy_${TRIVY_VERSION#v}_Linux-${TARCH}.tar.gz" | tar xz -C /usr/local/bin trivy; \
    curl -fsSLo /usr/local/bin/calicoctl "$(gh projectcalico/calico ${CALICOCTL_VERSION})/calicoctl-linux-${TARGETARCH}"; \
    curl -fsSL "$(gh cloudnative-pg/cloudnative-pg ${CNPG_VERSION})/kubectl-cnpg_${CNPG_VERSION#v}_linux_${XARCH}.tar.gz" | tar xz -C /usr/local/bin kubectl-cnpg; \
    chmod +x /usr/local/bin/*

# uv: instala cada herramienta Python en su propio venv (evita conflictos de dependencias)
COPY --from=uv /uv /uvx /usr/local/bin/

# Usuario sin privilegios (UID 1000 = típico usuario del host, para leer kubeconfig montado)
RUN useradd -m -u 1000 -s /bin/bash inspector
USER inspector
WORKDIR /home/inspector

ENV KREW_ROOT=/home/inspector/.krew \
    PATH="/home/inspector/.krew/bin:/home/inspector/.local/bin:${PATH}" \
    UV_LINK_MODE=copy \
    KUBECONFIG=/home/inspector/.kube/config \
    DATASTORE_TYPE=kubernetes \
    NETSHOOT_IMAGE=${NETSHOOT_IMAGE}

# Plugins de kubectl vía krew (con reintentos: descargan de GitHub, que a veces devuelve 503)
RUN set -eux; \
    case "$TARGETARCH" in amd64|arm64) ;; *) exit 1 ;; esac; \
    cd /tmp; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 "https://github.com/kubernetes-sigs/krew/releases/download/${KREW_VERSION}/krew-linux_${TARGETARCH}.tar.gz" | tar xz "./krew-linux_${TARGETARCH}"; \
    for i in 1 2 3 4 5; do \
      { "./krew-linux_${TARGETARCH}" install krew && kubectl krew install neat tree df-pv resource-capacity lineage; } && break; \
      [ "$i" = 5 ] && exit 1; sleep 15; \
    done; \
    rm -f "./krew-linux_${TARGETARCH}"; \
    rm -rf "${KREW_ROOT}/downloads" "${KREW_ROOT}/index/.git"

# Robusta KRR (right-sizing): no está en PyPI y su pyproject no es instalable con uv/pip,
# así que se clona y se usa en un venv propio (Python 3.11, exige <=3.12.9).
RUN git clone --depth 1 --branch "${KRR_REF}" https://github.com/robusta-dev/krr /home/inspector/krr \
    && uv venv --python 3.11 /home/inspector/krr/.venv \
    && uv pip install --python /home/inspector/krr/.venv/bin/python -r /home/inspector/krr/requirements.txt

# HolmesGPT (diagnóstico con LLM) sí está en PyPI
RUN uv tool install "holmesgpt==${HOLMESGPT_VERSION}" && uv cache clean

# Config de Holmes (se puede sobrescribir montando ./config/holmes.yaml)
COPY --chown=inspector:inspector config/holmes.yaml /home/inspector/.holmes/config.yaml

# Scripts propios
COPY --chown=inspector:inspector scripts/ /home/inspector/.local/bin/
COPY --chown=inspector:inspector scripts/bashrc /home/inspector/.bashrc

ENTRYPOINT ["/home/inspector/.local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
