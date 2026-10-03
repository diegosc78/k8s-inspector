FROM python:3.12-slim

ARG TARGETARCH=amd64

# Herramientas base de depuración
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash-completion ca-certificates curl git jq less vim-tiny \
    dnsutils iputils-ping netcat-openbsd openssl skopeo \
    && rm -rf /var/lib/apt/lists/*

# Binarios: kubectl, helm, k9s, stern, yq, kubectx/kubens (siempre última versión estable)
RUN set -eux; \
    latest() { curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$1/releases/latest" | sed 's#.*/tag/##'; }; \
    cd /tmp; \
    # kubectl
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${TARGETARCH}/kubectl"; \
    # helm
    HELM_V=$(latest helm/helm); \
    curl -fsSL "https://get.helm.sh/helm-${HELM_V}-linux-${TARGETARCH}.tar.gz" | tar xz --strip-components=1 -C /usr/local/bin "linux-${TARGETARCH}/helm"; \
    # k9s
    curl -fsSL "https://github.com/derailed/k9s/releases/latest/download/k9s_Linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin k9s; \
    # stern
    STERN_V=$(latest stern/stern); \
    curl -fsSL "https://github.com/stern/stern/releases/download/${STERN_V}/stern_${STERN_V#v}_linux_${TARGETARCH}.tar.gz" | tar xz -C /usr/local/bin stern; \
    # yq
    curl -fsSLo /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${TARGETARCH}"; \
    # kubectx / kubens
    for t in kubectx kubens; do \
      KX=$(latest ahmetb/kubectx); \
      ARCH=$([ "$TARGETARCH" = amd64 ] && echo x86_64 || echo arm64); \
      curl -fsSL "https://github.com/ahmetb/kubectx/releases/download/${KX}/${t}_${KX}_linux_${ARCH}.tar.gz" | tar xz -C /usr/local/bin "$t"; \
    done; \
    chmod +x /usr/local/bin/*

# uv: instala cada herramienta Python en su propio venv (evita conflictos de dependencias)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Usuario sin privilegios (UID 1000 = típico usuario del host, para leer kubeconfig montado)
RUN useradd -m -u 1000 -s /bin/bash inspector
USER inspector
WORKDIR /home/inspector

ENV PATH="/home/inspector/.local/bin:${PATH}" \
    UV_LINK_MODE=copy \
    KUBECONFIG=/home/inspector/.kube/config

# Robusta KRR (right-sizing): no está en PyPI y su pyproject no es instalable con uv/pip,
# así que se clona y se usa en un venv propio (Python 3.11, exige <=3.12.9). Fija KRR_REF a un tag/commit para reproducibilidad.
ARG KRR_REF=main
RUN git clone --depth 1 --branch "${KRR_REF}" https://github.com/robusta-dev/krr /home/inspector/krr \
    && uv venv --python 3.11 /home/inspector/krr/.venv \
    && uv pip install --python /home/inspector/krr/.venv/bin/python -r /home/inspector/krr/requirements.txt

# HolmesGPT (diagnóstico con LLM) sí está en PyPI
RUN uv tool install holmesgpt && uv cache clean

# Config de Holmes (se puede sobrescribir montando ./config/holmes.yaml)
COPY --chown=inspector:inspector config/holmes.yaml /home/inspector/.holmes/config.yaml

# Scripts propios
COPY --chown=inspector:inspector scripts/ /home/inspector/.local/bin/
COPY --chown=inspector:inspector scripts/bashrc /home/inspector/.bashrc

ENTRYPOINT ["/home/inspector/.local/bin/entrypoint.sh"]
CMD ["/bin/bash"]
