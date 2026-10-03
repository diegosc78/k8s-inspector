# k8s-inspector

Imagen Docker con todo lo necesario para **depurar y afinar (tuning) un clúster Kubernetes**, pensada para homelabs: herramientas habituales de línea de comandos, [Robusta KRR](https://github.com/robusta-dev/krr) para dimensionar requests/limits y [HolmesGPT](https://github.com/robusta-dev/holmesgpt) para diagnosticar problemas con un LLM.

**No contiene credenciales**: el kubeconfig se monta en solo lectura y las API keys se pasan por variables de entorno.

## Contenido de la imagen

| Herramienta | Para qué |
|---|---|
| `kubectl`, `helm`, `k9s`, `stern`, `kubectx`/`kubens` | Operar y explorar el clúster, logs multi-pod |
| `yq`, `jq`, `dig`, `nc`, `ping`, `openssl`, `skopeo` | Utilidades de apoyo y red |
| Plugins `kubectl` (krew): `neat`, `tree`, `df-pv`, `resource-capacity`, `lineage` | Manifiestos limpios, jerarquía de objetos, uso de PV, capacidad por nodo |
| `kubectl cnpg`, `calicoctl` | CloudNativePG y Calico (modo datastore `kubernetes`) |
| `popeye`, `kube-score`, `kubeconform`, `trivy` | Informe de buenas prácticas del clúster, calidad y validación de manifiestos, `trivy config` |
| `krr` (Robusta KRR) | Recomendaciones de requests/limits a partir de métricas de Prometheus |
| `holmes` (HolmesGPT) | Diagnóstico asistido por LLM (usa `kubectl`, logs y Prometheus) |

Scripts propios (en el `PATH`):

| Script | Descripción |
|---|---|
| `health` | Resumen rápido: nodos, consumo, pods no sanos, reinicios altos y últimos Warnings |
| `pf` | Port-forward en segundo plano a Prometheus (`pf stop` para pararlo) |
| `krr-run [args]` | Ejecuta `krr simple` contra Prometheus (abre el port-forward si hace falta) |
| `holmes-ask "pregunta"` | Ejecuta `holmes ask` con acceso a Prometheus |
| `netshoot <pod\|node/x>` | Contenedor efímero de red (`kubectl debug` con `nicolaka/netshoot`) |

El contenedor corre como usuario no root (`inspector`, UID 1000).

## Requisitos

- Docker (con Compose)
- Un kubeconfig con acceso al clúster
- Una API key de algún proveedor LLM (solo para HolmesGPT; KRR y el resto no la necesitan)

## Inicio rápido

```bash
git clone <url-de-este-repo> && cd k8s-inspector

cp .env.example .env        # rellena tu API key y el MODEL
docker compose pull         # imagen publicada: ponte124/k8s-inspector (o `docker compose build`)
docker compose run --rm inspector
```

Dentro del contenedor:

```bash
health                                        # estado general
k9s                                           # UI de terminal
krr-run -n kube-system                        # recomendaciones de recursos de un namespace
holmes-ask "¿hay algún pod con problemas?"    # diagnóstico con LLM
```

## Configuración

### Credenciales de Kubernetes

Por defecto se monta `~/.kube/config` en solo lectura. Para usar otro fichero:

```bash
export KUBECONFIG_HOST=/ruta/a/mi/kubeconfig
```

**Recomendado: ServiceAccount de solo lectura.** [k8s/rbac.yaml](k8s/rbac.yaml) crea el namespace `k8s-inspector`, un ServiceAccount con el ClusterRole `view` + un rol extra de lectura (nodos, PV, RBAC, métricas, CRDs de Calico/CNPG; **sin Secrets**) y el único permiso de escritura necesario: `pods/portforward` en el namespace `monitoring` (ajústalo si tu Prometheus está en otro).

```bash
kubectl apply -f k8s/rbac.yaml
./k8s/make-kubeconfig.sh > ~/.kube/k8s-inspector.config && chmod 600 ~/.kube/k8s-inspector.config
export KUBECONFIG_HOST=~/.kube/k8s-inspector.config
```

El token no caduca; para revocarlo: `kubectl delete -f k8s/rbac.yaml`.

> El kubeconfig debe ser legible por el UID 1000 del contenedor.

### LLM (HolmesGPT) — `.env`

HolmesGPT usa [LiteLLM](https://docs.litellm.ai), así que admite muchos proveedores. Ejemplos:

```env
# OpenRouter (proveedor nativo de LiteLLM)
OPENROUTER_API_KEY=sk-or-...
MODEL=openrouter/anthropic/claude-sonnet-4.5

# OpenRouter u otro endpoint compatible con OpenAI
# OPENAI_API_KEY=...
# OPENAI_API_BASE=https://openrouter.ai/api/v1
# MODEL=openai/anthropic/claude-sonnet-4.5

# Ollama local (sin API key)
# MODEL=ollama/llama3.1
# OLLAMA_API_BASE=http://host.docker.internal:11434
```

Elige un modelo con buen soporte de *tool calling*: Holmes lo usa intensivamente y los modelos pequeños o gratuitos suelen diagnosticar peor.

### Prometheus

Dos opciones:

- **Port-forward automático (por defecto).** `pf` abre `kubectl port-forward` dentro del contenedor usando estas variables de `.env`:
  ```env
  PROM_NAMESPACE=monitoring
  PROM_SERVICE=prometheus-operated    # o kube-prometheus-stack-prometheus
  PROM_PORT=9090
  ```
  Para localizar el servicio: `kubectl get svc -A | grep -i prom`.
- **Prometheus ya expuesto** (ingress, LoadBalancer…): define `PROMETHEUS_URL=http://...` y no se usará el port-forward.

El **metrics-server** no necesita port-forward: `kubectl top` va por el API server.

### Toolsets de HolmesGPT — `config/holmes.yaml`

Holmes intenta activar ~20 integraciones y marca como fallidas las que no aplican (Cilium, OpenShift, AKS, ArgoCD…). Este repo trae un [config/holmes.yaml](config/holmes.yaml) que desactiva las que no se usan en un clúster kubeadm genérico y activa `prometheus/metrics`. **Ajústalo a tu clúster**: si usas ArgoCD, Cilium, etc., quita la línea correspondiente (algunas necesitan además binarios o variables de entorno adicionales).

El fichero se monta desde `./config/holmes.yaml`, así que puedes editarlo sin reconstruir la imagen. Ver estado de los toolsets: `holmes toolset list`.

### Red

Si el API server o Prometheus solo son alcanzables a través de la red del host (VPN, etc.), descomenta `network_mode: host` en [docker-compose.yml](docker-compose.yml).

## Sin Compose

```bash
docker run --rm -it \
  --env-file .env \
  -v ~/.kube/config:/home/inspector/.kube/config:ro \
  -v "$PWD/config/holmes.yaml:/home/inspector/.holmes/config.yaml:ro" \
  ponte124/k8s-inspector
```

## Build y publicación

El [Makefile](Makefile) usa `docker buildx` (multi-arquitectura amd64 + arm64) y publica en Docker Hub. El repositorio destino es configurable (por defecto `ponte124`):

```bash
make build                          # imagen local (arquitectura del host)
docker login                        # (o `make login`)
make push                           # ponte124/k8s-inspector:latest
make push REPO=otro TAG=1.0.0       # otro/k8s-inspector:1.0.0 (+ :latest)
make push BUILD_ARGS="--build-arg KUBECTL_VERSION=v1.37.1"
```

Variables: `REPO`, `IMAGE`, `TAG`, `PLATFORMS`, `BUILDER`, `BUILD_ARGS`. `make help` las muestra.

### Versiones fijadas

Todas las herramientas tienen un `ARG` con versión concreta en el [Dockerfile](Dockerfile), con valores por defecto adecuados para un **clúster Kubernetes 1.36** (`kubectl` v1.36.5; admite ±1 versión menor respecto al API server). Al actualizar el clúster, sube `KUBECTL_VERSION` y revisa `K9S_VERSION`/`HELM_VERSION`/`KRR_REF`/`HOLMESGPT_VERSION`. Los plugins de krew se instalan en su última versión del índice.

Notas:

- **KRR** no está en PyPI y su `pyproject.toml` no es instalable con pip/uv, por lo que se clona (tag `KRR_REF`) y se instala desde `requirements.txt` en un venv con Python 3.11 (KRR exige Python ≤ 3.12.9).
- **HolmesGPT** se instala desde PyPI con `uv tool install`, en su propio entorno aislado.

## Seguridad

- Ninguna credencial se incluye en la imagen; `.gitignore` y `.dockerignore` excluyen `.env`, kubeconfigs y claves.
- Monta el kubeconfig con `:ro` y usa el ServiceAccount de **solo lectura** de [k8s/rbac.yaml](k8s/rbac.yaml): HolmesGPT puede ejecutar `kubectl` y no debe poder modificar nada.
- El contenido de tu clúster (logs, eventos, manifiestos) se envía al proveedor LLM que configures. Revisa qué datos sensibles pueden contener antes de usarlo con un proveedor externo, o usa un modelo local (Ollama).

## Estructura

```
.
├── Dockerfile
├── Makefile              # buildx multi-arch + push a Docker Hub
├── docker-compose.yml
├── k8s/                  # rbac.yaml (ServiceAccount solo lectura) y make-kubeconfig.sh
├── .env.example          # plantilla de variables (copiar a .env)
├── config/holmes.yaml    # toolsets de HolmesGPT
└── scripts/              # health, pf, krr-run, holmes-ask, netshoot, krr, bashrc, entrypoint
```

## Roadmap

Ideas pendientes, de mayor a menor prioridad.

### Seguridad y mantenimiento
- [x] **Kubeconfig de solo lectura:** ServiceAccount con el ClusterRole `view` y un kubeconfig dedicado para el contenedor (HolmesGPT ejecuta `kubectl`, no debería tener permisos de admin).
- [x] **Versiones fijadas** (hecho). Pendiente: automatizar las actualizaciones con Renovate o Dependabot.
- [ ] **CI:** GitHub Actions que construya la imagen, la analice con `hadolint` y `trivy` y la publique en GHCR.
- [x] **Multi-arquitectura:** build con `docker buildx` para amd64 y arm64 (el Dockerfile ya usa `TARGETARCH`).

### Automatización
- [ ] **Informes guardados:** que `krr-run` vuelque a `~/reports/` con fecha (`--formatter json`/`csv`) para comparar recomendaciones en el tiempo.
- [ ] **Ejecución no interactiva:** `holmes-ask` y `krr-run` desde cron o un systemd timer, con informe periódico por correo o Telegram.
- [ ] **HolmesGPT por alertas:** ejecutarlo como servicio (o servidor MCP) integrado con Alertmanager para que investigue cuando salte una alerta.

### Herramientas a añadir
- [x] **Plugins de `kubectl` vía krew:** `neat`, `tree`, `df-pv`, `resource-capacity`, `lineage` (este último reactiva el toolset `kubernetes/krew-extras` de Holmes).
- [x] **Bases de datos:** plugin `kubectl cnpg` (si usas CloudNativePG).
- [x] **Red:** `calicoctl` (si usas Calico) e imagen efímera tipo `netshoot` para `kubectl debug`.
- [x] **Calidad de manifiestos:** `popeye` o `kube-score` (informe de buenas prácticas del clúster) y `kubeconform` / `trivy config`.

### Integraciones de HolmesGPT
- [ ] **Grafana:** activar el toolset de dashboards.
