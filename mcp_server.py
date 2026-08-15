"""MCP server multi-repo: expõe grafos indexados de um GitHub store para clientes MCP.

Uso:
    python mcp_server.py             # stdio (padrão)

Env vars (todas opcionais, defaults sensatos):
    MCP_GRAPH_STORE_DIR    Diretório local do clone do store
                            (default: ~/.mcp-graph-store/repo)
    MCP_GRAPH_STORE_URL    URL git para clonar se STORE_DIR não existir
                            (default: https://github.com/pedrothor/mcp-graph-store.git)
    MCP_GRAPH_CACHE_MB     Cache LRU em MB para graph.json.gz descompactados
                            (default: 200)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

import networkx as nx
from mcp.server.fastmcp import FastMCP

from build_index import build_index
from ingest import extract_to, parse_github_url, repo_slug

STORE_DIR = Path(
    os.environ.get(
        "MCP_GRAPH_STORE_DIR",
        str(Path.home() / ".mcp-graph-store" / "repo"),
    )
).resolve()
STORE_URL = os.environ.get(
    "MCP_GRAPH_STORE_URL", "https://github.com/pedrothor/mcp-graph-store.git"
)
CACHE_MB = int(os.environ.get("MCP_GRAPH_CACHE_MB", "200"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-server")

mcp = FastMCP("repos-graph")


# --------------------------------------------------------------------------
# SizedLRU: cache com limite por bytes (não por número de entradas)
# --------------------------------------------------------------------------

class SizedLRU:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._store: OrderedDict[str, tuple[Any, int]] = OrderedDict()
        self._total = 0

    def get(self, key: str) -> Any | None:
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key][0]
        return None

    def put(self, key: str, value: Any, size: int) -> None:
        if key in self._store:
            _, old_size = self._store.pop(key)
            self._total -= old_size
        while self._store and self._total + size > self.max_bytes:
            _, (_, ev_size) = self._store.popitem(last=False)
            self._total -= ev_size
        self._store[key] = (value, size)
        self._total += size

    def invalidate(self, key: str) -> None:
        if key in self._store:
            _, size = self._store.pop(key)
            self._total -= size

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._store), "bytes": self._total, "limit_bytes": self.max_bytes}


_graph_cache = SizedLRU(max_bytes=CACHE_MB * 1024 * 1024)
_nx_cache = SizedLRU(max_bytes=CACHE_MB * 1024 * 1024)


# --------------------------------------------------------------------------
# Store bootstrap: clone se preciso, garante estrutura mínima
# --------------------------------------------------------------------------

def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    log.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def _ensure_store() -> None:
    """Garante que STORE_DIR existe, é um repo git em branch main, com estrutura mínima."""
    if not STORE_DIR.exists():
        if not STORE_URL:
            raise RuntimeError(
                f"MCP_GRAPH_STORE_DIR não existe ({STORE_DIR}) e MCP_GRAPH_STORE_URL não setada"
            )
        STORE_DIR.parent.mkdir(parents=True, exist_ok=True)
        log.info("clonando store %s → %s", STORE_URL, STORE_DIR)
        _git("clone", STORE_URL, str(STORE_DIR))

    if not (STORE_DIR / ".git").exists():
        raise RuntimeError(f"{STORE_DIR} não é um repositório git")

    # Garante que HEAD aponta para main (funciona mesmo em repo vazio)
    head_check = _git("rev-parse", "--verify", "HEAD", cwd=STORE_DIR, check=False)
    if head_check.returncode != 0:
        # repo vazio: apenas seta HEAD simbolicamente pra main
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=STORE_DIR)
    else:
        # tem commits: se não está em main, renomeia branch atual pra main
        current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=STORE_DIR).stdout.strip()
        if current != "main":
            _git("branch", "-M", "main", cwd=STORE_DIR)

    # Estrutura mínima
    (STORE_DIR / "repos").mkdir(exist_ok=True)
    index_path = STORE_DIR / "index.json"
    if not index_path.exists():
        index_path.write_text('{"repos": {}}', encoding="utf-8")
    gitignore = STORE_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            # apenas o graph.json.gz e o meta.json são úteis; o resto é interno do graphify
            "**/graphify-out/cache/\n"
            "**/graphify-out/manifest.json\n"
            "**/graphify-out/.graphify_analysis.json\n"
            "**/graphify-out/*.html\n",
            encoding="utf-8",
        )


def _pull_store() -> None:
    """Best-effort git pull da main. Erros só logam warning (comum em repo vazio)."""
    try:
        _git("pull", "--rebase", "origin", "main", cwd=STORE_DIR)
    except subprocess.CalledProcessError as e:
        log.warning("git pull falhou: %s", (e.stderr or "").strip()[:200])


# --------------------------------------------------------------------------
# Leitura de grafos (com cache LRU por bytes)
# --------------------------------------------------------------------------

def _graph_gz_path(repo: str) -> Path:
    p = STORE_DIR / "repos" / repo / "graphify-out" / "graph.json.gz"
    if not p.exists():
        raise ValueError(f"graph.json.gz não encontrado para '{repo}' em {p}")
    return p


def _load_graph(repo: str) -> dict[str, Any]:
    cached = _graph_cache.get(repo)
    if cached is not None:
        return cached
    path = _graph_gz_path(repo)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    size = path.stat().st_size * 8  # estimativa: gz descompactado ~8× o gz
    _graph_cache.put(repo, data, size)
    return data


def _load_nx(repo: str) -> nx.MultiDiGraph:
    cached = _nx_cache.get(repo)
    if cached is not None:
        return cached
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
    # estimativa grosseira do tamanho: nodes + edges × 200 bytes
    size = (g.number_of_nodes() + g.number_of_edges()) * 200
    _nx_cache.put(repo, g, size)
    return g


def _load_index() -> dict[str, Any]:
    path = STORE_DIR / "index.json"
    if not path.exists():
        return {"repos": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _node_display_name(node: dict[str, Any]) -> str:
    return str(node.get("label") or node.get("norm_label") or node.get("id") or "")


def _find_node(data: dict[str, Any], query: str) -> dict[str, Any] | None:
    q = query.lower()
    for node in data.get("nodes", []):
        if node.get("id") == query:
            return node
    for node in data.get("nodes", []):
        if _node_display_name(node).lower() == q:
            return node
    return None


# --------------------------------------------------------------------------
# Tools de LEITURA
# --------------------------------------------------------------------------

@mcp.tool()
def list_repos() -> list[str]:
    """Lista slugs de todos os repos presentes no store."""
    index = _load_index()
    if index.get("repos"):
        return sorted(index["repos"].keys())
    # fallback: varre disco
    repos_dir = STORE_DIR / "repos"
    if not repos_dir.exists():
        return []
    return sorted(
        d.name for d in repos_dir.iterdir()
        if d.is_dir() and (d / "graphify-out" / "graph.json.gz").exists()
    )


@mcp.tool()
def describe_repos() -> dict[str, Any]:
    """Retorna o index.json inteiro: metadados leves de todos os repos.

    Use isto ANTES de chamar tools mais pesadas (search_symbol, get_neighbors) —
    o LLM decide em qual repo procurar sem custar I/O de grafo.
    """
    return _load_index()


@mcp.tool()
def get_repo_summary(repo: str) -> dict[str, Any]:
    """Retorna a entrada de `repo` no index.json (stats + hubs + comunidades)."""
    index = _load_index()
    entry = index.get("repos", {}).get(repo)
    if entry is None:
        raise ValueError(f"repo desconhecido: {repo}")
    return entry


@mcp.tool()
def get_node(repo: str, node_id: str) -> dict[str, Any]:
    """Retorna um node por id exato ou label (case-insensitive)."""
    data = _load_graph(repo)
    node = _find_node(data, node_id)
    if node is None:
        raise ValueError(f"node não encontrado: {node_id}")
    return node


@mcp.tool()
def search_symbol(
    pattern: str, repo: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Busca símbolos por substring no label ou id (case-insensitive).

    Se repo=None, busca em TODOS os repos indexados (cross-repo).
    """
    pattern_lc = pattern.lower()
    targets = [repo] if repo else list_repos()
    results: list[dict[str, Any]] = []
    for r in targets:
        try:
            data = _load_graph(r)
        except ValueError:
            continue
        for node in data.get("nodes", []):
            haystack = f"{_node_display_name(node)} {node.get('id', '')}".lower()
            if pattern_lc in haystack:
                results.append({"repo": r, **node})
                if len(results) >= limit:
                    return results
    return results


@mcp.tool()
def get_neighbors(repo: str, node_id: str, depth: int = 1) -> dict[str, Any]:
    """Vizinhos de um node até profundidade N (in + out)."""
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
    """Menor caminho entre dois símbolos (por id ou label)."""
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
    """Comunidades (agrupamentos Leiden) inferidas do campo `community` dos nodes."""
    data = _load_graph(repo)
    groups: dict[Any, list[str]] = {}
    for node in data.get("nodes", []):
        c = node.get("community")
        if c is None:
            continue
        groups.setdefault(c, []).append(_node_display_name(node))

    result = [
        {"community_id": cid, "size": len(labels), "sample_labels": labels[:8]}
        for cid, labels in groups.items()
    ]
    result.sort(key=lambda x: x["size"], reverse=True)
    return result[:top_n]


# --------------------------------------------------------------------------
# Tools de ESCRITA (extraem + publicam no store)
# --------------------------------------------------------------------------

def _push_store(commit_msg: str) -> str:
    """Faz add + commit + push origin main. Retorna o SHA do commit ou 'no-changes'."""
    _git("add", ".", cwd=STORE_DIR)
    status = _git("status", "--porcelain", cwd=STORE_DIR)
    if not status.stdout.strip():
        return "no-changes"
    _git("commit", "-m", commit_msg, cwd=STORE_DIR)
    push = _git("push", "-u", "origin", "main", cwd=STORE_DIR, check=False)
    if push.returncode != 0:
        log.warning("push falhou, rebase + retry: %s", (push.stderr or "").strip()[:200])
        _git("pull", "--rebase", "origin", "main", cwd=STORE_DIR, check=False)
        _git("push", "-u", "origin", "main", cwd=STORE_DIR)
    return _git("rev-parse", "HEAD", cwd=STORE_DIR).stdout.strip()


@mcp.tool()
def index_repo(url: str, force: bool = False) -> dict[str, Any]:
    """Extrai o grafo de um repo GitHub, publica no store, atualiza index.json.

    Args:
        url: URL do repositório (https ou ssh)
        force: se True, re-extrai mesmo com SHA remoto igual ao já indexado
    """
    owner, repo = parse_github_url(url)
    slug = repo_slug(owner, repo)

    _pull_store()

    out_dir = STORE_DIR / "repos" / slug
    result = extract_to(url, out_dir, force=force)

    if result["status"] == "failed":
        return result

    if result["status"] == "skipped":
        return {**result, "commit_pushed": None}

    # reconstrói index.json com todos os repos do store
    build_index(STORE_DIR)

    # invalida cache do repo (caso já estivesse em memória de indexação anterior)
    _graph_cache.invalidate(slug)
    _nx_cache.invalidate(slug)

    commit_sha = result.get("commit_sha", "?")[:8]
    pushed_sha = _push_store(f"Index {slug} @ {commit_sha}")

    return {
        **result,
        "graph_path": str(result["graph_path"]),
        "commit_pushed": pushed_sha,
    }


@mcp.tool()
def remove_repo(slug: str) -> dict[str, Any]:
    """Remove um repo do store: deleta pasta, atualiza index.json, faz push."""
    import shutil
    _pull_store()
    target = STORE_DIR / "repos" / slug
    if not target.exists():
        return {"status": "not_found", "slug": slug}

    shutil.rmtree(target)
    build_index(STORE_DIR)
    _graph_cache.invalidate(slug)
    _nx_cache.invalidate(slug)

    pushed_sha = _push_store(f"Remove {slug}")
    return {"status": "removed", "slug": slug, "commit_pushed": pushed_sha}


@mcp.tool()
def refresh_store() -> dict[str, Any]:
    """git pull no clone local do store. Útil quando outra máquina publicou."""
    before = _git("rev-parse", "HEAD", cwd=STORE_DIR, check=False).stdout.strip()
    _pull_store()
    after = _git("rev-parse", "HEAD", cwd=STORE_DIR).stdout.strip()

    if before != after:
        # invalida cache — grafos podem ter mudado
        for slug in list_repos():
            _graph_cache.invalidate(slug)
            _nx_cache.invalidate(slug)

    return {
        "before_sha": before,
        "after_sha": after,
        "changed": before != after,
        "cache": _graph_cache.stats(),
    }


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("STORE_DIR = %s", STORE_DIR)
    log.info("STORE_URL = %s", STORE_URL)
    log.info("cache LRU = %d MB", CACHE_MB)
    _ensure_store()
    _pull_store()
    log.info("MCP server pronto (stdio)")
    mcp.run()
