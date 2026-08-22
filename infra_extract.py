"""Extractors de arquivos de infra pra virarem nodes no mesmo grafo dos repos de código.

MVP: Kubernetes manifests. Cada `kind: X, metadata.name: Y` vira um node.
Pra adicionar Serverless/CloudFormation/Terraform, crie novo `extract_<tipo>()`
que devolve `(nodes, links)` no mesmo formato.

Formato dos nodes é compatível com o schema já usado pelo graphify:
    id, label, source_file, source_location, file_type, _origin, + extras
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger("infra_extract")

# Extensões de código conhecidas pelo graphify — usado só na detecção de
# `has_code` em list_repo_files. Fonte: docs do graphify.
_CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".java", ".kt", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".m",
}
_YAML_EXTS = {".yaml", ".yml"}
_TERRAFORM_EXTS = {".tf", ".tfvars"}


def _looks_like_k8s(doc: dict) -> bool:
    """Um doc YAML é K8s se tem apiVersion + kind + metadata.name."""
    if not isinstance(doc, dict):
        return False
    return bool(doc.get("apiVersion") and doc.get("kind")
                and isinstance(doc.get("metadata"), dict)
                and doc["metadata"].get("name"))


def extract_k8s(files: Iterable[Path], repo_root: Path) -> tuple[list[dict], list[dict]]:
    """Parseia YAMLs K8s e devolve (nodes, links).

    Cada resource vira um node com id `k8s:<namespace>:<kind>:<name>`.
    Links MVP:
      - Ingress.spec.rules[].http.paths[].backend.service.name -> Service
      - Deployment/StatefulSet/DaemonSet -> ConfigMap/Secret referenciados
        em envFrom/env[].valueFrom
    """
    nodes: list[dict] = []
    links: list[dict] = []
    seen_ids: set[str] = set()

    for path in files:
        if not path.exists():
            log.warning("infra_files: arquivo não existe: %s", path)
            continue
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        try:
            with path.open(encoding="utf-8") as f:
                docs = list(yaml.safe_load_all(f))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            log.warning("infra_files: falha ao parsear %s: %s", rel, e)
            continue

        for doc_idx, doc in enumerate(docs):
            if not _looks_like_k8s(doc):
                continue
            meta = doc["metadata"]
            kind = str(doc["kind"])
            name = str(meta["name"])
            namespace = str(meta.get("namespace") or "default")
            nid = f"k8s:{namespace}:{kind}:{name}"
            if nid in seen_ids:
                # duplicado no mesmo repo — mantém o primeiro
                continue
            seen_ids.add(nid)

            nodes.append({
                "id": nid,
                "label": f"{kind}/{name}",
                "norm_label": f"{kind.lower()}/{name.lower()}",
                "source_file": rel,
                "source_location": {"doc_index": doc_idx},
                "file_type": "k8s_manifest",
                "_origin": "infra_k8s",
                "k8s_kind": kind,
                "k8s_namespace": namespace,
                "k8s_name": name,
                "k8s_api_version": str(doc.get("apiVersion", "")),
                "k8s_labels": meta.get("labels") or {},
            })
            links.extend(_k8s_links(doc, nid, namespace, rel))

    return nodes, links


def _k8s_links(doc: dict, nid: str, namespace: str, rel_file: str) -> list[dict]:
    """Extrai links a partir de um doc K8s. Best-effort, tolerante a schemas variados."""
    links: list[dict] = []
    kind = doc.get("kind")

    # Ingress -> Service
    if kind == "Ingress":
        for rule in (doc.get("spec", {}).get("rules") or []):
            for path_entry in (rule.get("http", {}).get("paths") or []):
                backend = path_entry.get("backend", {})
                svc = backend.get("service") or {}
                svc_name = svc.get("name")
                if svc_name:
                    links.append({
                        "source": nid,
                        "target": f"k8s:{namespace}:Service:{svc_name}",
                        "relation": "routes_to",
                        "source_file": rel_file,
                    })

    # Workloads -> ConfigMap/Secret via env
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
        containers = _walk_containers(doc)
        for c in containers:
            for env in (c.get("env") or []):
                ref = env.get("valueFrom") or {}
                cm_ref = ref.get("configMapKeyRef")
                sec_ref = ref.get("secretKeyRef")
                if cm_ref and cm_ref.get("name"):
                    links.append({
                        "source": nid,
                        "target": f"k8s:{namespace}:ConfigMap:{cm_ref['name']}",
                        "relation": "reads_env",
                        "source_file": rel_file,
                    })
                if sec_ref and sec_ref.get("name"):
                    links.append({
                        "source": nid,
                        "target": f"k8s:{namespace}:Secret:{sec_ref['name']}",
                        "relation": "reads_env",
                        "source_file": rel_file,
                    })
            for env_from in (c.get("envFrom") or []):
                cm = env_from.get("configMapRef") or {}
                sec = env_from.get("secretRef") or {}
                if cm.get("name"):
                    links.append({
                        "source": nid,
                        "target": f"k8s:{namespace}:ConfigMap:{cm['name']}",
                        "relation": "reads_envfrom",
                        "source_file": rel_file,
                    })
                if sec.get("name"):
                    links.append({
                        "source": nid,
                        "target": f"k8s:{namespace}:Secret:{sec['name']}",
                        "relation": "reads_envfrom",
                        "source_file": rel_file,
                    })

    return links


def _walk_containers(doc: dict) -> list[dict]:
    """Retorna a lista de containers, cobrindo Deployment/DaemonSet/StatefulSet/Job/CronJob."""
    spec = doc.get("spec") or {}
    # CronJob tem spec.jobTemplate.spec.template.spec.containers
    template = (
        spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
        or spec.get("template", {})
    )
    pod_spec = template.get("spec") or {}
    return (pod_spec.get("containers") or []) + (pod_spec.get("initContainers") or [])


def classify_files(repo_root: Path) -> dict[str, Any]:
    """Varre repo_root e devolve inventário + flags de detecção.

    Retorno pensado pro LLM decidir se chama index_repo com/sem infra_files.
    """
    files: list[dict] = []
    yaml_files: list[str] = []
    has_code = False
    has_terraform = False

    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue
        # ignora .git
        try:
            rel_parts = p.relative_to(repo_root).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] == ".git":
            continue
        rel = "/".join(rel_parts)
        ext = p.suffix.lower()
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        files.append({"path": rel, "ext": ext, "size": size})
        if ext in _CODE_EXTS:
            has_code = True
        if ext in _YAML_EXTS:
            yaml_files.append(rel)
        if ext in _TERRAFORM_EXTS:
            has_terraform = True

    return {
        "num_files": len(files),
        "files": files,
        "yaml_files": yaml_files,
        "detected": {
            "has_code": has_code,
            "has_yaml": bool(yaml_files),
            "has_terraform": has_terraform,
        },
    }
