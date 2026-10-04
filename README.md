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
| `pf [prometheus\|grafana]` | Port-forward en segundo plano a Prometheus (por defecto) o Grafana (`pf stop` los para) |
| `krr-run [args]` | Ejecuta `krr inspector` (simple + tus estándares de [config/krr.yaml](config/krr.yaml)) contra Prometheus (abre el port-forward si hace falta) |
| `krr-report [args]` | Como `krr-run`, pero guarda el JSON con fecha en `~/reports/` (volumen `./reports`) |
| `krr-diff [a.json b.json]` | Compara dos informes (por defecto los dos últimos) y muestra qué recomendaciones han cambiado |
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
krr-report && krr-diff                        # guarda informe con fecha y compara con el anterior
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

Además define **toolsets propios** (solo lectura) para las herramientas extra de la imagen, que Holmes no trae integradas:

| Toolset | Qué le permite a Holmes |
|---|---|
| `krr/recommendations` | Recomendaciones de requests/limits con KRR OSS local (`krr-run`). No es el toolset `robusta`, que consulta la plataforma Robusta SaaS y está desactivado |
| `popeye/sanitizer` | Informe de salud y buenas prácticas del clúster |
| `calico/core` | IP pools, IPAM, políticas de red y endpoints de Calico |
| `cnpg/core` | Estado, réplicas y backups de clústeres CloudNativePG |

Se desactivan solos si falta el binario o las CRDs del clúster. `kubernetes/krew-extras` es nativo de Holmes y se activa solo.

El fichero se monta desde `./config/holmes.yaml`, así que puedes editarlo sin reconstruir la imagen. Ver estado de los toolsets: `holmes toolset list`.

### Estándares de KRR — `config/krr.yaml`

KRR no tiene fichero de configuración propio (solo flags), así que la imagen registra una estrategia `inspector` (= `simple` + reglas propias, [scripts/krr_inspector.py](scripts/krr_inspector.py)) que lee [config/krr.yaml](config/krr.yaml): mínimos de CPU/memoria (`100m` / `64Mi`) y un límite de CPU heurístico en lugar de «unset»: `clamp(max(request × cpu_factor, pico observado × peak_factor), cpu_min, tope)`, siempre ≥ request (por defecto ×2, pico ×1,25, desde 500m; el pico evita estrangular cargas a ráfagas). El tope es `min(cpu_max, cores del worker más pequeño × (1 − node_margin))`: los cores asignables (`allocatable`) se leen de la API de K8s (nodos sin control-plane, margen extra del 10 %; ya cubierto por `k8s/rbac.yaml`; si falla, solo se aplica `cpu_max`). `cpu_factor: 0` restaura el límite sin definir. Los flags de la CLI (`--cpu-min`, `--cpu_percentile`…) tienen prioridad sobre el YAML.

La imagen lleva estos valores por defecto en `/home/inspector/.krr/config.yaml`; para cambiarlos, monta tu fichero encima (`-v "$PWD/config/krr.yaml:/home/inspector/.krr/config.yaml:ro"`, ya hecho en `docker-compose.yml`) o apunta `KRR_CONFIG` a otra ruta.

### Informes guardados

`krr-report` guarda cada ejecución en `./reports/krr-AAAAMMDD-HHMMSS[-namespace].json` (el directorio se monta como volumen, ignorado por git). Tras varias ejecuciones, `krr-diff` muestra qué contenedores han cambiado su recomendación más de `MIN_CHANGE`% (10 por defecto) y cuáles son nuevos o han desaparecido:

```bash
krr-report -n kube-system        # hoy
krr-report -n kube-system        # dentro de una semana
MIN_CHANGE=5 krr-diff            # compara los dos últimos
```

> Compara informes con el mismo alcance (mismo `-n`) y la misma ventana de histórico.

### Grafana (opcional)

Holmes puede buscar y leer dashboards de Grafana (toolset `grafana/dashboards`). Necesita un token de **solo lectura**:

1. En Grafana: *Administration → Users and access → Service accounts → Add service account* (rol **Viewer**) → *Add service account token*.
2. Pon el token en `.env`: `GRAFANA_API_KEY=glsa_...` (`holmes-ask` abre el port-forward a Grafana solo; configurable con `GRAFANA_NAMESPACE`/`GRAFANA_SERVICE`/`GRAFANA_PORT`, o define `GRAFANA_URL` si ya está expuesto).
3. Descomenta el bloque `grafana/dashboards` de [config/holmes.yaml](config/holmes.yaml). Va comentado porque Holmes evalúa las variables de entorno aunque el toolset esté desactivado y mostraría errores en cada arranque si faltan.

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

Todas las herramientas tienen un `ARG` con versión concreta en el [Dockerfile](Dockerfile), con valores por defecto adecuados para un **clúster Kubernetes 1.36** (`kubectl` v1.36.5; admite ±1 versión menor respecto al API server). `CALICOCTL_VERSION` debe coincidir con la versión de Calico del clúster (calicoctl rechaza versiones distintas). Al actualizar el clúster, sube `KUBECTL_VERSION` y revisa `K9S_VERSION`/`HELM_VERSION`/`KRR_REF`/`HOLMESGPT_VERSION`. Los plugins de krew se instalan en su última versión del índice.

Calicoctl debe coincidir con la versión de Calico del clúster. Ejemplo:
ARG CALICOCTL_VERSION=v3.30.2

Si en tu clúster tienes otra versión, cambia el ARG y construye tu imagen

Notas:

- **KRR** no está en PyPI y su `pyproject.toml` no es instalable con pip/uv, por lo que se clona (tag `KRR_REF`) y se instala desde `requirements.txt` en un venv con Python 3.11 (KRR exige Python ≤ 3.12.9).
- **HolmesGPT** se instala desde PyPI con `uv tool install`, en su propio entorno aislado.

## API de Holmes y compatibilidad OpenAI / Anthropic

La misma imagen sirve para dos usos: **cliente** (shell interactiva, por defecto) y **servidor** (API dentro del clúster). Solo cambia el comando:

| Comando | Qué arranca | Puerto |
|---|---|---|
| `bash` (por defecto) | Shell con todas las herramientas | - |
| `holmes-server` | API nativa de Holmes (`/api/chat`...) | 5050 |
| `holmes-gateway` | Adaptador **OpenAI** (`/v1/chat/completions`, `/v1/models`) y **Anthropic** (`/v1/messages`) delante de Holmes | 8080 |

El `server.py` de Holmes no viene en el paquete de PyPI: el [Dockerfile](Dockerfile) lo toma del repo en el tag exacto de `HOLMESGPT_VERSION`.

### Probar en local

```bash
# en .env: HOLMES_API_KEY=... y GATEWAY_API_KEY=... (cualquier cadena)
docker compose --profile api up holmes-server holmes-gateway
curl localhost:8080/v1/models -H "Authorization: Bearer $GATEWAY_API_KEY"
curl localhost:8080/v1/chat/completions -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"holmes","stream":true,"messages":[{"role":"user","content":"¿hay pods con problemas?"}]}'
```

Fuera del clúster, `holmes-server` abre solo los port-forward a Prometheus/Grafana (como `holmes-ask`). Dentro del clúster (`KUBERNETES_SERVICE_HOST` definido) no los abre: usa el ServiceAccount del pod y el `PROMETHEUS_URL` que le pases (o el autodescubrimiento de Holmes). El ServiceAccount de solo lectura de [k8s/rbac.yaml](k8s/rbac.yaml) sirve también para el pod.

### Qué hace el gateway

- **Indicadores de progreso.** Holmes tarda decenas de segundos en investigar. Con `stream: true` el gateway emite los comandos que va ejecutando (`🔧 kubectl get nodes`), su razonamiento (`💭 ...`) y un latido cada `HEARTBEAT_SECONDS` (`⏳ Sigo trabajando… (35 s)`). Van como `reasoning_content` (OpenAI; Open WebUI lo muestra como bloque plegable de "pensamiento") o como bloque `thinking` (Anthropic). `PROGRESS_MODE=content` los mezcla en la respuesta; `off` solo envía keepalives (`: keepalive` / `ping`). Sin `stream`, envía espacios en blanco (JSON válido) hasta tener la respuesta, para que los proxies no corten por inactividad.
- **Memoria de herramientas.** Los clientes solo reenvían el texto de la conversación, así que Holmes "olvidaría" qué consultó. El gateway guarda el historial completo de Holmes (con llamadas y resultados de herramientas) en una caché en memoria, localizada por el último par usuario/asistente, y lo recupera en el turno siguiente. Si no está en caché (reinicio, varias réplicas, mensaje editado) recurre al historial de solo texto. Es una caché por proceso: con varias réplicas, usa afinidad de sesión o una sola.
- **Tareas auxiliares sin Holmes.** Open WebUI lanza peticiones propias (título del chat, etiquetas, preguntas de seguimiento, consultas de búsqueda, autocompletado) a través del mismo modelo. El gateway las reconoce por el texto de sus plantillas y las envía **directamente al LLM subyacente** (el `MODEL` de Holmes, vía `litellm`), sin herramientas ni investigación. Quedan en el log: `Tarea auxiliar -> LLM directo (...)`. Si el LLM falla no se recurre a Holmes. La plantilla RAG de Open WebUI (preguntas con documentos adjuntos) no se desvía: va a Holmes.
- **Herramientas del cliente ignoradas.** Holmes ejecuta sus herramientas en el servidor; no se exponen como `tool_calls`.
- **Streaming.** Holmes no emite tokens sueltos: la respuesta final llega por trozos al terminar la investigación.

| Variable | Defecto | Descripción |
|---|---|---|
| `HOLMES_URL` | `http://localhost:5050` | Servidor Holmes (en k8s, sidecar o Service) |
| `HOLMES_API_KEY` | vacío | Clave de la API de Holmes (en servidor y gateway) |
| `GATEWAY_API_KEY` | vacío (sin auth) | Clave de los clientes (`Authorization: Bearer` o `x-api-key`) |
| `PROGRESS_MODE` | `reasoning` | `reasoning` \| `content` \| `off` |
| `HEARTBEAT_SECONDS` | `10` | Intervalo del latido |
| `REASONING_MAX_CHARS` | `300` | Recorte del razonamiento mostrado (0 = no mostrarlo) |
| `HISTORY_CACHE_MAX` / `HISTORY_CACHE_TTL` | `64` / `21600` | Entradas y segundos de la caché de historial |
| `GATEWAY_PORT` | `8080` | Puerto del gateway |
| `TASK_BYPASS` | `true` | `false` envía también las tareas auxiliares a Holmes |
| `STRIP_MARKERS` | `true` | Quita de la respuesta los marcadores `<< {"type": "promql", ...} >>` de Holmes (solo los interpreta la UI de Robusta) |
| `TASK_PATTERNS` / `TASK_PATTERNS_EXTRA` | plantillas de Open WebUI | Expresiones regulares (separadas por `;;`) que identifican tareas auxiliares; la primera sustituye a las de serie, la segunda las amplía (útil si personalizas las plantillas) |
| `DEFAULT_MODEL_ID` | `holmes` | Alias del modelo por defecto de Holmes |

### Open WebUI

*Admin → Settings → Connections → OpenAI*: URL `http://<servicio>:8080/v1`, clave `GATEWAY_API_KEY`, modelo `holmes`. Los títulos, etiquetas y sugerencias que genera Open WebUI no llegan a Holmes: el gateway los desvía al LLM subyacente (ver arriba). Si personalizas esas plantillas en Open WebUI, añade su frase en `TASK_PATTERNS_EXTRA`; como red de seguridad, puedes además asignar un *modelo de tareas* distinto en *Settings → Interface*.

### Despliegue en Kubernetes

Esta imagen no incluye manifiestos (el chart de Helm vive aparte). Puntos a tener en cuenta: dos contenedores de la misma imagen en el pod (`holmes-server` y `holmes-gateway`, hablando por `localhost`) o dos Deployments; ConfigMap con [config/holmes.yaml](config/holmes.yaml) montado en `/home/inspector/.holmes/config.yaml`; Secret con la API key del LLM y las claves; `PROMETHEUS_URL` con la URL del Service; y subir los timeouts del Gateway/Ingress si lo expones fuera del clúster.

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
├── gateway/              # adaptador OpenAI/Anthropic para la API de Holmes
├── k8s/                  # rbac.yaml (ServiceAccount solo lectura) y make-kubeconfig.sh
├── .env.example          # plantilla de variables (copiar a .env)
├── config/holmes.yaml    # toolsets de HolmesGPT
├── config/krr.yaml       # estándares de dimensionado de KRR (mínimos, límite de CPU)
└── scripts/              # health, pf, krr-run, holmes-ask, holmes-server, holmes-gateway, netshoot, krr-report, krr-diff, krr, bashrc, entrypoint
```
