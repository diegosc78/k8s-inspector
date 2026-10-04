# Build multi-arquitectura y publicación en Docker Hub.
#   make push                      -> ponte124/k8s-inspector:latest (amd64+arm64)
#   make push REPO=otro TAG=1.0.0  -> otro/k8s-inspector:1.0.0
#   make build                     -> imagen local (arquitectura del host)
REPO       ?= ponte124
IMAGE      ?= k8s-inspector
TAG        ?= 26.10.4
PLATFORMS  ?= linux/amd64,linux/arm64
BUILDER    ?= multi-arch-builder
BUILD_ARGS ?=
# Ejemplo: make build BUILD_ARGS="--build-arg KUBECTL_VERSION=v1.37.1"

FULL := $(REPO)/$(IMAGE):$(TAG)
# Si TAG != latest, publica también :latest
EXTRA_TAG := $(if $(filter-out latest,$(TAG)),-t $(REPO)/$(IMAGE):latest,)

.PHONY: help builder login build push run lint

help:
	@echo "Targets: builder login build push run lint"
	@echo "Variables: REPO=$(REPO) IMAGE=$(IMAGE) TAG=$(TAG) PLATFORMS=$(PLATFORMS)"

# Crea (si no existe) el builder buildx y registra QEMU para compilar arm64 en amd64
builder:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 || docker buildx create --name $(BUILDER) --driver docker-container --use
	@docker buildx use $(BUILDER)
	@docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null

login:
	docker login

# Build local de una sola plataforma, cargada en el daemon
build:
	docker buildx build --load -t $(FULL) $(BUILD_ARGS) .

# Build multi-arch y publicación
push: builder
	docker buildx build --platform $(PLATFORMS) --push -t $(FULL) $(EXTRA_TAG) $(BUILD_ARGS) .

run:
	IMAGE=$(REPO)/$(IMAGE) TAG=$(TAG) docker compose run --rm inspector

lint:
	docker run --rm -i hadolint/hadolint < Dockerfile
