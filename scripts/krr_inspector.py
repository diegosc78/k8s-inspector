"""Entrada de KRR con la estrategia `inspector`: `simple` + estándares leídos de un YAML (config/krr.yaml).

- requests.cpu_min / requests.memory_min -> se inyectan como --cpu-min / --mem-min (si no se pasan por CLI).
- limits.*  -> el límite de CPU deja de ser "unset": clamp(max(request * cpu_factor, pico_observado * peak_factor), cpu_min, tope),
  siempre >= request.
  tope = min(cpu_max, cores del worker más pequeño * (1 - node_margin)); los cores se leen de la API de K8s.
- strategy.* -> valores por defecto de los parámetros de la estrategia (la CLI los sobrescribe).
"""
import math
import os
import re
import sys

sys.path.insert(0, os.path.expanduser("~/krr"))

import numpy as np
import pydantic as pd
import yaml

import robusta_krr
from robusta_krr.api.models import K8sObjectData, MetricsPodData, ResourceType, RunResult
from robusta_krr.api.strategies import BaseStrategy
from robusta_krr.core.integrations.prometheus.metrics import PercentileCPULoader
from robusta_krr.strategies.simple import SimpleStrategy, SimpleStrategySettings

CONFIG_PATH = os.environ.get("KRR_CONFIG", os.path.expanduser("~/.krr/config.yaml"))


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def quantity(value, default: float, unit: str) -> float:
    """'100m' -> 0.1 (cores) · '2' -> 2 · '64Mi' -> 64 (MiB). unit: 'cpu' (cores) o 'mem' (MiB)."""
    if value is None:
        return default
    m = re.fullmatch(r"\s*([\d.]+)\s*(m|Ki|Mi|Gi|Ti|k|M|G)?\s*", str(value))
    if not m:
        raise SystemExit(f"krr_inspector: cantidad inválida '{value}' en {CONFIG_PATH}")
    n, suffix = float(m.group(1)), m.group(2)
    if unit == "cpu":
        return n / 1000 if suffix == "m" else n
    mib = {None: 1 / 1024**2, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024**2, "k": 1e3 / 1024**2, "M": 1e6 / 1024**2, "G": 1e9 / 1024**2}
    return n * mib[suffix]


CFG = load_config()
REQ, LIM, STRAT = CFG.get("requests", {}), CFG.get("limits", {}), CFG.get("strategy", {})
CPU_FACTOR = float(LIM.get("cpu_factor", 0))
CPU_LIM_MIN = quantity(LIM.get("cpu_min"), 0, "cpu")
CPU_LIM_MAX = quantity(LIM.get("cpu_max"), math.inf, "cpu")
PEAK_FACTOR = float(LIM.get("peak_factor", 0))  # 0 = ignorar el pico observado
PEAK_PERCENTILE = float(LIM.get("peak_percentile", 100))

# Pico de CPU observado (se registra por nombre de clase: no puede llamarse PercentileCPULoader, ya lo usa `simple`)
PeakCPULoader = PercentileCPULoader(PEAK_PERCENTILE)
PeakCPULoader.__name__ = "PeakCPULoader"
NODE_MARGIN = float(LIM.get("node_margin", 0.10))  # < 0 desactiva el tope por nodo


def _argv_value(*names: str):
    for i, a in enumerate(sys.argv):
        if a in names and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        for n in names:
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


_node_cap: float | None = None
_node_cap_done = False


def node_cpu_cap() -> float | None:
    """Cores asignables (allocatable) del worker más pequeño menos el margen (None si no se puede consultar o está desactivado)."""
    global _node_cap, _node_cap_done
    if _node_cap_done or NODE_MARGIN < 0:
        return _node_cap
    _node_cap_done = True
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(config_file=_argv_value("-k", "--kubeconfig"), context=_argv_value("-c", "--context"))
        nodes = client.CoreV1Api().list_node().items
        control = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
        workers = [n for n in nodes if not any(l in (n.metadata.labels or {}) for l in control)] or nodes
        cores = min(quantity(n.status.allocatable["cpu"], 0, "cpu") for n in workers)
        _node_cap = cores * (1 - NODE_MARGIN)
        print(f"krr_inspector: worker más pequeño = {cores:g} cores -> tope de límite de CPU {_node_cap * 1000:g}m", file=sys.stderr)
    except Exception as e:  # sin acceso a la API: se usa solo cpu_max
        print(f"krr_inspector: no se pudo leer la capacidad de los nodos ({e}); tope por nodo desactivado", file=sys.stderr)
    return _node_cap


def cpu_limit_cap() -> float:
    return min(CPU_LIM_MAX, node_cpu_cap() or math.inf)


class InspectorSettings(SimpleStrategySettings):
    cpu_percentile: float = pd.Field(STRAT.get("cpu_percentile", 95), gt=0, le=100, description="Percentil de CPU para el request.")
    memory_buffer_percentage: float = pd.Field(
        STRAT.get("memory_buffer_percentage", 15), gt=0, description="Buffer (%) sobre el pico de memoria."
    )


class InspectorStrategy(BaseStrategy[InspectorSettings]):
    """simple + límite de CPU heurístico acotado. Hereda de BaseStrategy (KRR solo registra subclases directas)
    y delega el cálculo base en una instancia de SimpleStrategy."""

    display_name = "inspector"
    rich_console = True

    def __init__(self, settings):
        super().__init__(settings)
        self._simple = SimpleStrategy(settings)

    @property
    def metrics(self):
        return self._simple.metrics + ([PeakCPULoader] if PEAK_FACTOR > 0 else [])

    @property
    def description(self):
        if CPU_FACTOR <= 0:
            return self._simple.description
        return (
            f"Como `simple`, pero el límite de CPU = clamp(max(request x {CPU_FACTOR:g}, pico x {PEAK_FACTOR:g}), "
            f"{CPU_LIM_MIN * 1000:g}m, {cpu_limit_cap() * 1000:g}m), siempre >= request. Config: {CONFIG_PATH}"
        )

    def run(self, history_data: MetricsPodData, object_data: K8sObjectData) -> RunResult:
        result = self._simple.run(history_data, object_data)
        cpu = result[ResourceType.CPU]
        if CPU_FACTOR > 0 and cpu.request is not None and not math.isnan(cpu.request):
            candidates = [cpu.request * CPU_FACTOR, CPU_LIM_MIN]
            peaks = [np.max(v[:, 1]) for v in history_data.get("PeakCPULoader", {}).values() if len(v)]
            if PEAK_FACTOR > 0 and peaks:
                candidates.append(float(np.max(peaks)) * PEAK_FACTOR)
            limit = min(max(candidates), cpu_limit_cap())
            cpu.limit = max(limit, cpu.request)
        return result


def inject_min_flags(argv: list[str]) -> list[str]:
    """--cpu-min (millicores) y --mem-min (MiB) son opciones globales de KRR, no de la estrategia."""
    if len(argv) < 2 or argv[1].startswith("-") or "--help" in argv:
        return argv
    extra = []
    if "--cpu-min" not in argv and REQ.get("cpu_min") is not None:
        extra += ["--cpu-min", str(math.ceil(quantity(REQ["cpu_min"], 0, "cpu") * 1000))]
    if "--mem-min" not in argv and REQ.get("memory_min") is not None:
        extra += ["--mem-min", str(math.ceil(quantity(REQ["memory_min"], 0, "mem")))]
    return argv + extra


if __name__ == "__main__":
    sys.argv = inject_min_flags(sys.argv)
    robusta_krr.run()
