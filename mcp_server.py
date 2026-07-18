"""MCP server multi-repo: expõe grafos gerados por ingest.py para clientes MCP.

Uso:
    python mcp_server.py             # stdio (padrão)
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import networkx as nx
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
REPOS_DIR = ROOT / "repos"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mcp-server")

mcp = FastMCP("repos-graph")


def _repo_dir(repo: str) -> Path:
    d = REPOS_DIR / repo
    if not d.is_dir():
        raise ValueError(f"repo desconhecido: {repo}")
    return d


def _graph_path(repo: str) -> Path:
    p = _repo_dir(repo) / "graphify-out" / "graph.json"
    if not p.exists():
        raise ValueError(f"graph.json não encontrado para {repo}")
    return p


@lru_cache(maxsize=64)
def _load_graph(repo: str) -> dict[str, Any]:
    with _graph_path(repo).open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=64)
def _load_nx(repo: str) -> nx.MultiDiGraph:
    """Constrói um networkx a partir do graph.json (schema networkx: nodes + links)."""
    data = _load_graph(repo)
    g = nx.MultiDiGraph()
    for node in data.get("nodes", []):
        nid = node.get("id")
        if nid is None:
            continue
        g.add_node(nid, **{k: v for k, v in node.items() if k != "id"})
    for link in data.get("links", []):
        src = link.get("source")
        tgt = link.get("target")
        if src is None or tgt is None:
            continue
        attrs = {k: v for k, v in link.items() if k not in ("source", "target")}
        g.add_edge(src, tgt, **attrs)
    return g


def _node_display_name(node: dict[str, Any]) -> str:
    return str(node.get("label") or node.get("norm_label") or node.get("id") or "")


def _find_node(data: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Busca node por id exato ou por label (case-insensitive)."""
    q = query.lower()
    for node in data.get("nodes", []):
        if node.get("id") == query:
            return node
    for node in data.get("nodes", []):
        if _node_display_name(node).lower() == q:
            return node
    return None


@mcp.tool()
def list_repos() -> list[str]:
    """Lista todos os repos indexados (aqueles com graphify-out/graph.json presente)."""
    if not REPOS_DIR.exists():
        return []
    result: list[str] = []
    for d in sorted(REPOS_DIR.iterdir()):
        if d.is_dir() and (d / "graphify-out" / "graph.json").exists():
            result.append(d.name)
    return result


@mcp.tool()
def get_repo_summary(repo: str) -> dict[str, Any]:
    """Retorna metadados + estatísticas + report markdown (se existir) do repo."""
    d = _repo_dir(repo)
    meta_path = d / "meta.json"
    report_path = d / "graphify-out" / "GRAPH_REPORT.md"
    data = _load_graph(repo)

    communities: set[Any] = set()
    file_types: dict[str, int] = {}
    for node in data.get("nodes", []):
        c = node.get("community")
        if c is not None:
            communities.add(c)
        ft = node.get("file_type") or "unknown"
        file_types[ft] = file_types.get(ft, 0) + 1

    summary: dict[str, Any] = {
        "repo": repo,
        "num_nodes": len(data.get("nodes", [])),
        "num_links": len(data.get("links", [])),
        "num_communities": len(communities),
        "file_types": file_types,
    }
    if meta_path.exists():
        summary["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
    if report_path.exists():
        summary["report_md"] = report_path.read_text(encoding="utf-8")
    return summary


@mcp.tool()
def get_node(repo: str, node_id: str) -> dict[str, Any]:
    """Retorna um node por id exato ou nome (case-insensitive)."""
    data = _load_graph(repo)
    node = _find_node(data, node_id)
    if node is None:
        raise ValueError(f"node não encontrado: {node_id}")
    return node


@mcp.tool()
def search_symbol(pattern: str, repo: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Busca símbolos por substring no label ou id (case-insensitive).

    Se repo=None, busca em todos os repos indexados.
    """
    pattern_lc = pattern.lower()
    targets = [repo] if repo else list_repos()
    results: list[dict[str, Any]] = []
    for r in targets:
        data = _load_graph(r)
        for node in data.get("nodes", []):
            haystack = f"{_node_display_name(node)} {node.get('id', '')}".lower()
            if pattern_lc in haystack:
                results.append({"repo": r, **node})
                if len(results) >= limit:
                    return results
    return results


@mcp.tool()
def get_neighbors(repo: str, node_id: str, depth: int = 1) -> dict[str, Any]:
    """Vizinhos de um node até profundidade N (direções: saída e entrada)."""
    g = _load_nx(repo)
    data = _load_graph(repo)
    node = _find_node(data, node_id)
    if node is None:
        raise ValueError(f"node não encontrado: {node_id}")
    nid = node.get("id")

    visited: set[str] = {nid}
    frontier: set[str] = {nid}
    for _ in range(max(1, depth)):
        next_frontier: set[str] = set()
        for n in frontier:
            if n in g:
                next_frontier.update(g.successors(n))
                next_frontier.update(g.predecessors(n))
        next_frontier -= visited
        visited |= next_frontier
        frontier = next_frontier
        if not frontier:
            break

    nodes = [{"id": n, **g.nodes[n]} for n in visited if n in g]
    edges: list[dict[str, Any]] = []
    for u, v, attrs in g.edges(data=True):
        if u in visited and v in visited:
            edges.append({"source": u, "target": v, **attrs})
    return {"root": nid, "depth": depth, "nodes": nodes, "edges": edges}


@mcp.tool()
def shortest_path(repo: str, source: str, target: str) -> dict[str, Any]:
    """Menor caminho entre dois símbolos (por id ou nome)."""
    g = _load_nx(repo)
    data = _load_graph(repo)
    s_node = _find_node(data, source)
    t_node = _find_node(data, target)
    if s_node is None or t_node is None:
        raise ValueError("source ou target não encontrado")
    s = s_node.get("id")
    t = t_node.get("id")

    ug = g.to_undirected(as_view=True)
    try:
        path = nx.shortest_path(ug, source=s, target=t)
    except nx.NetworkXNoPath:
        return {"source": s, "target": t, "path": None, "reason": "sem caminho"}
    except nx.NodeNotFound as e:
        return {"source": s, "target": t, "path": None, "reason": str(e)}

    return {"source": s, "target": t, "length": len(path) - 1, "path": path}


@mcp.tool()
def list_communities(repo: str, top_n: int = 20) -> list[dict[str, Any]]:
    """Lista comunidades (agrupamentos Leiden) inferidas do campo `community` dos nodes.

    Retorna até top_n comunidades ordenadas por tamanho, com samples de labels.
    """
    data = _load_graph(repo)
    groups: dict[Any, list[str]] = {}
    for node in data.get("nodes", []):
        c = node.get("community")
        if c is None:
            continue
        groups.setdefault(c, []).append(_node_display_name(node))

    result = [
        {
            "community_id": cid,
            "size": len(labels),
            "sample_labels": labels[:8],
        }
        for cid, labels in groups.items()
    ]
    result.sort(key=lambda x: x["size"], reverse=True)
    return result[:top_n]


if __name__ == "__main__":
    log.info("MCP server iniciando (stdio) — repos em %s", REPOS_DIR)
    mcp.run()
