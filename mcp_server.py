"""MCP server multi-repo. Suporta 3 providers de storage do grafo:

- local_git   (default) — clone git em disco + push local. Bom para dev.
- github      — leitura HTTPS de raw.githubusercontent.com. Stateless (K8s).
- azure_devops — leitura via REST API do Azure DevOps. Stateless.

Env vars principais:
    MCP_STORE_PROVIDER   local_git | github | azure_devops   (default: local_git)
    MCP_GRAPH_CACHE_MB   Cache LRU para grafos descompactados em RAM (default: 200)
    MCP_INDEX_TTL_SEC    TTL do cache do index.json em RAM   (default: 300)

    # local_git
    MCP_GRAPH_STORE_DIR  Path do clone local (default: ~/.mcp-graph-store/repo)
    MCP_GRAPH_STORE_URL  URL git para clonar se dir não existir

    # github  (ver github_store.py)
    MCP_GITHUB_REPO, MCP_GITHUB_BRANCH, MCP_GITHUB_TOKEN

    # azure_devops  (ver azure_devops_store.py)
    MCP_AZDO_ORG, MCP_AZDO_PROJECT, MCP_AZDO_REPO, MCP_AZDO_BRANCH, MCP_AZDO_PAT
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

import networkx as nx
from mcp.server.fastmcp import FastMCP

PROVIDER = os.environ.get("MCP_STORE_PROVIDER", "local_git").lower()
CACHE_MB = int(os.environ.get("MCP_GRAPH_CACHE_MB", "200"))
INDEX_TTL_SEC = int(os.environ.get("MCP_INDEX_TTL_SEC", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-server")

mcp = FastMCP("repos-graph")


# --------------------------------------------------------------------------
# SizedLRU: cache com limite por bytes
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

    def clear(self) -> None:
        self._store.clear()
        self._total = 0

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._store), "bytes": self._total, "limit_bytes": self.max_bytes}


_graph_cache = SizedLRU(max_bytes=CACHE_MB * 1024 * 1024)
_nx_cache = SizedLRU(max_bytes=CACHE_MB * 1024 * 1024)


# --------------------------------------------------------------------------
# Store Protocol + factory
# --------------------------------------------------------------------------

class Store(Protocol):
    def get_index(self) -> dict[str, Any]: ...
    def get_graph(self, slug: str, commit_sha: str | None) -> dict[str, Any]: ...
    def can_write(self) -> bool: ...
    def refresh(self) -> None: ...
    def describe(self) -> dict[str, Any]: ...


# ----- LocalGitStore (V3 behavior) -----------------------------------------

class LocalGitStore:
    def __init__(self) -> None:
        self.dir = Path(
            os.environ.get(
                "MCP_GRAPH_STORE_DIR", str(Path.home() / ".mcp-graph-store" / "repo")
            )
        ).resolve()
        self.url = os.environ.get(
            "MCP_GRAPH_STORE_URL", "https://github.com/pedrothor/mcp-graph-store.git"
        )
        self._ensure()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.dir if self.dir.exists() else None,
            capture_output=True, text=True, check=check,
        )

    def _ensure(self) -> None:
        if not self.dir.exists():
            if not self.url:
                raise RuntimeError(f"{self.dir} não existe e MCP_GRAPH_STORE_URL não setada")
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            log.info("clonando store %s → %s", self.url, self.dir)
            subprocess.run(
                ["git", "clone", self.url, str(self.dir)],
                capture_output=True, text=True, check=True,
            )
        if not (self.dir / ".git").exists():
            raise RuntimeError(f"{self.dir} não é um repositório git")
        head = self._git("rev-parse", "--verify", "HEAD", check=False)
        if head.returncode != 0:
            self._git("symbolic-ref", "HEAD", "refs/heads/main")
        else:
            cur = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if cur != "main":
                self._git("branch", "-M", "main")
        (self.dir / "repos").mkdir(exist_ok=True)
        idx = self.dir / "index.json"
        if not idx.exists():
            idx.write_text('{"repos": {}}', encoding="utf-8")
        gitignore = self.dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "**/graphify-out/cache/\n"
                "**/graphify-out/manifest.json\n"
                "**/graphify-out/.graphify_analysis.json\n"
                "**/graphify-out/*.html\n",
                encoding="utf-8",
            )

    def get_index(self) -> dict[str, Any]:
        p = self.dir / "index.json"
        if not p.exists():
            return {"repos": {}}
        return json.loads(p.read_text(encoding="utf-8"))

    def get_graph(self, slug: str, commit_sha: str | None = None) -> dict[str, Any]:
        p = self.dir / "repos" / slug / "graphify-out" / "graph.json.gz"
        if not p.exists():
            raise ValueError(f"graph.json.gz não encontrado para '{slug}'")
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)

    def can_write(self) -> bool:
        return True

    def refresh(self) -> None:
        # git pull best-effort
        self._git("pull", "--rebase", "origin", "main", check=False)

    def describe(self) -> dict[str, Any]:
        return {"kind": "local_git", "dir": str(self.dir), "url": self.url}

    def push(self, commit_msg: str) -> str:
        """Add + commit + push. Retorna SHA ou 'no-changes'."""
        self._git("add", ".")
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return "no-changes"
        self._git("commit", "-m", commit_msg)
        push = self._git("push", "-u", "origin", "main", check=False)
        if push.returncode != 0:
            log.warning("push falhou, rebase + retry: %s", (push.stderr or "").strip()[:200])
            self._git("pull", "--rebase", "origin", "main", check=False)
            self._git("push", "-u", "origin", "main")
        return self._git("rev-parse", "HEAD").stdout.strip()


# ----- Remote (HTTPS) providers via github_store / azure_devops_store ------

class RemoteStoreAdapter:
    """Wraps GitHubStore or AzureDevOpsStore. Read-only. Sem git no runtime."""

    def __init__(self, backend: Any) -> None:
        self._b = backend
        self._index_cache: dict[str, Any] | None = None
        self._index_ts: float = 0.0

    def get_index(self) -> dict[str, Any]:
        now = time.time()
        if self._index_cache is not None and (now - self._index_ts) < INDEX_TTL_SEC:
            return self._index_cache
        log.info("refresh index from %s", self._b.describe())
        self._index_cache = self._b.fetch_index()
        self._index_ts = now
        return self._index_cache

    def get_graph(self, slug: str, commit_sha: str | None = None) -> dict[str, Any]:
        return self._b.fetch_graph(slug)

    def can_write(self) -> bool:
        return False

    def refresh(self) -> None:
        self._index_cache = None
        _graph_cache.clear()
        _nx_cache.clear()

    def describe(self) -> dict[str, Any]:
        return self._b.describe()


def _make_store() -> Store:
    if PROVIDER == "github":
        from github_store import GitHubStore
        return RemoteStoreAdapter(GitHubStore())
    if PROVIDER == "azure_devops":
        from azure_devops_store import AzureDevOpsStore
        return RemoteStoreAdapter(AzureDevOpsStore())
    return LocalGitStore()


_store: Store = _make_store()


# --------------------------------------------------------------------------
# Graph loading (com cache LRU, keyed por slug@sha)
# --------------------------------------------------------------------------

def _graph_cache_key(slug: str) -> str:
    idx = _store.get_index()
    entry = idx.get("repos", {}).get(slug, {})
    sha = entry.get("commit_sha", "?")
    return f"{slug}@{sha[:12]}"


def _load_graph(slug: str) -> dict[str, Any]:
    key = _graph_cache_key(slug)
    cached = _graph_cache.get(key)
    if cached is not None:
        return cached
    data = _store.get_graph(slug)
    # tamanho estimado do dict serializado
    size = len(json.dumps(data, default=str).encode("utf-8"))
    _graph_cache.put(key, data, size)
    return data


def _load_nx(slug: str) -> nx.MultiDiGraph:
    key = _graph_cache_key(slug)
    cached = _nx_cache.get(key)
    if cached is not None:
        return cached
    data = _load_graph(slug)
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
    size = (g.number_of_nodes() + g.number_of_edges()) * 200
    _nx_cache.put(key, g, size)
    return g


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
# Tools de LEITURA (funcionam em todos os providers)
# --------------------------------------------------------------------------

@mcp.tool()
def list_repos() -> list[str]:
    """Lista slugs de todos os repos presentes no store. Não baixa nenhum grafo."""
    return sorted(_store.get_index().get("repos", {}).keys())


@mcp.tool()
def describe_repos() -> dict[str, Any]:
    """Retorna o index inteiro (stats + hubs + comunidades por repo).

    Use ANTES de tools mais pesadas para o LLM decidir onde procurar.
    Não inclui o search_index bruto (pode ser grande) — para busca use search_symbol.
    """
    idx = _store.get_index()
    # não retorna search_index (pode inflar muito o payload); resto do summary vai
    trimmed = {"generated_at_utc": idx.get("generated_at_utc"), "repos": {}}
    for slug, entry in idx.get("repos", {}).items():
        trimmed["repos"][slug] = {
            k: v for k, v in entry.items() if k != "search_index"
        }
    return trimmed


@mcp.tool()
def get_repo_summary(repo: str) -> dict[str, Any]:
    """Entrada do repo no index.json (sem o search_index bruto)."""
    idx = _store.get_index()
    entry = idx.get("repos", {}).get(repo)
    if entry is None:
        raise ValueError(f"repo desconhecido: {repo}")
    return {k: v for k, v in entry.items() if k != "search_index"}


@mcp.tool()
def search_symbol(
    pattern: str, repo: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Busca símbolos por substring no label ou id (case-insensitive).

    - Se `repo=None` (cross-repo): usa `search_index` do index.json — não baixa
      nenhum graph.json.gz. Extremamente barato.
    - Se `repo` especificado: baixa o grafo do repo (cache LRU) e faz busca completa
      no nodes[] (retorna todos os campos do node).
    """
    pattern_lc = pattern.lower()
    results: list[dict[str, Any]] = []

    if repo is None:
        idx = _store.get_index()
        for slug, entry in idx.get("repos", {}).items():
            for sym in entry.get("search_index", []):
                hay = f"{sym.get('label', '')} {sym.get('id', '')}".lower()
                if pattern_lc in hay:
                    results.append({"repo": slug, **sym})
                    if len(results) >= limit:
                        return results
        return results

    data = _load_graph(repo)
    for node in data.get("nodes", []):
        haystack = f"{_node_display_name(node)} {node.get('id', '')}".lower()
        if pattern_lc in haystack:
            results.append({"repo": repo, **node})
            if len(results) >= limit:
                return results
    return results


@mcp.tool()
def get_node(repo: str, node_id: str) -> dict[str, Any]:
    """Detalhes de um node por id exato ou label."""
    data = _load_graph(repo)
    node = _find_node(data, node_id)
    if node is None:
        raise ValueError(f"node não encontrado: {node_id}")
    return node


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
        nxt: set[str] = set()
        for n in frontier:
            if n in g:
                nxt.update(g.successors(n))
                nxt.update(g.predecessors(n))
        nxt -= visited
        visited |= nxt
        frontier = nxt
        if not frontier:
            break
    nodes = [{"id": n, **g.nodes[n]} for n in visited if n in g]
    edges = [
        {"source": u, "target": v, **a}
        for u, v, a in g.edges(data=True) if u in visited and v in visited
    ]
    return {"root": nid, "depth": depth, "nodes": nodes, "edges": edges}


@mcp.tool()
def shortest_path(repo: str, source: str, target: str) -> dict[str, Any]:
    """Menor caminho entre dois símbolos."""
    g = _load_nx(repo)
    data = _load_graph(repo)
    s_node = _find_node(data, source)
    t_node = _find_node(data, target)
    if s_node is None or t_node is None:
        raise ValueError("source ou target não encontrado")
    s, t = s_node.get("id"), t_node.get("id")
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
    """Comunidades Leiden inferidas do campo `community` dos nodes."""
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


@mcp.tool()
def store_info() -> dict[str, Any]:
    """Info sobre o provider ativo + estatísticas de cache."""
    return {
        "provider": _store.describe(),
        "can_write": _store.can_write(),
        "graph_cache": _graph_cache.stats(),
        "index_ttl_sec": INDEX_TTL_SEC,
    }


@mcp.tool()
def refresh_store() -> dict[str, Any]:
    """Força refresh do index e limpa cache LRU. Útil após indexação externa."""
    _store.refresh()
    return {"ok": True, "provider": _store.describe(), "cache": _graph_cache.stats()}


# --------------------------------------------------------------------------
# Tools de ESCRITA (apenas se can_write() — hoje, só local_git)
# --------------------------------------------------------------------------

def _require_writable() -> None:
    if not _store.can_write():
        raise RuntimeError(
            f"provider '{PROVIDER}' é read-only. "
            "Use MCP_STORE_PROVIDER=local_git para indexação, "
            "ou rode ingest via CLI/CI e publique no store."
        )


@mcp.tool()
def index_repo(url: str, force: bool = False) -> dict[str, Any]:
    """Extrai o grafo de um repo GitHub, publica no store, atualiza index.json.

    Disponível apenas quando MCP_STORE_PROVIDER=local_git.
    Em produção (github/azure_devops), rode a indexação num job externo.
    """
    _require_writable()
    from build_index import build_index
    from ingest import extract_to, parse_github_url, repo_slug

    owner, repo = parse_github_url(url)
    slug = repo_slug(owner, repo)

    assert isinstance(_store, LocalGitStore)
    _store.refresh()

    out_dir = _store.dir / "repos" / slug
    result = extract_to(url, out_dir, force=force)

    if result["status"] == "failed":
        return result
    if result["status"] == "skipped":
        return {**result, "commit_pushed": None}

    build_index(_store.dir)
    _graph_cache.invalidate(_graph_cache_key(slug))
    _nx_cache.invalidate(_graph_cache_key(slug))

    commit_sha = result.get("commit_sha", "?")[:8]
    pushed_sha = _store.push(f"Index {slug} @ {commit_sha}")
    return {**result, "graph_path": str(result["graph_path"]), "commit_pushed": pushed_sha}


@mcp.tool()
def remove_repo(slug: str) -> dict[str, Any]:
    """Remove um repo do store + push. Apenas em local_git."""
    _require_writable()
    import shutil
    from build_index import build_index

    assert isinstance(_store, LocalGitStore)
    _store.refresh()
    target = _store.dir / "repos" / slug
    if not target.exists():
        return {"status": "not_found", "slug": slug}
    shutil.rmtree(target)
    build_index(_store.dir)
    _graph_cache.invalidate(_graph_cache_key(slug))
    _nx_cache.invalidate(_graph_cache_key(slug))
    pushed_sha = _store.push(f"Remove {slug}")
    return {"status": "removed", "slug": slug, "commit_pushed": pushed_sha}


# --------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("provider = %s", _store.describe())
    log.info("cache LRU = %d MB, index TTL = %d s", CACHE_MB, INDEX_TTL_SEC)
    log.info("MCP server pronto (stdio)")
    mcp.run()
