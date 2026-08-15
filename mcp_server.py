"""MCP server multi-repo. Storage: MongoDB (via mongo_store.py).

Env vars:
    MONGODB_URI          conexão (default: mongodb://localhost:27017/)
    MONGODB_DB           database (default: elos_agent)
    MONGODB_COLLECTION   collection (default: mcp)
    MCP_ALLOW_WRITES     "true" habilita a tool index_repo no runtime.
                         Em produção deixe unset e faça publish via CI/CLI.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from mongo_store import MongoStore

ALLOW_WRITES = os.environ.get("MCP_ALLOW_WRITES", "").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp-server")

mcp = FastMCP("repos-graph")

_store = MongoStore()


# --------------------------------------------------------------------------
# Tools de LEITURA
# --------------------------------------------------------------------------

@mcp.tool()
def list_repos() -> list[str]:
    """Slugs de todos os repos presentes no store."""
    return _store.list_repo_slugs()


@mcp.tool()
def describe_repos() -> dict[str, Any]:
    """Retorna metadados leves de todos os repos: stats + top hubs + top communities.

    Use ANTES de tools mais pesadas para o LLM decidir onde procurar.
    """
    return _store.get_index()


@mcp.tool()
def get_repo_summary(repo: str) -> dict[str, Any]:
    """Metadados + stats do repo (top hubs, top communities, file_types, url, commit)."""
    doc = _store.get_repo_summary(repo)
    if doc is None:
        raise ValueError(f"repo desconhecido: {repo}")
    return doc


@mcp.tool()
def search_symbol(
    pattern: str, repo: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Busca símbolos por substring no label ou node_id (case-insensitive).

    Se `repo=None`, busca em TODOS os repos (cross-repo). Uma única query MongoDB.
    """
    return _store.search_symbol(pattern, slug=repo, limit=limit)


@mcp.tool()
def get_node(repo: str, node_id: str) -> dict[str, Any]:
    """Detalhes de um node por id exato ou label (case-insensitive)."""
    node = _store.get_node(repo, node_id)
    if node is None:
        raise ValueError(f"node não encontrado: {node_id}")
    return node


@mcp.tool()
def get_neighbors(repo: str, node_id: str, depth: int = 1) -> dict[str, Any]:
    """Vizinhos de um node até profundidade N (in + out) via BFS no Mongo."""
    return _store.get_neighbors(repo, node_id, depth=depth)


@mcp.tool()
def shortest_path(repo: str, source: str, target: str) -> dict[str, Any]:
    """Menor caminho entre dois símbolos (BFS undirected, cap de 20 níveis)."""
    return _store.shortest_path(repo, source, target)


@mcp.tool()
def list_communities(repo: str, top_n: int = 20) -> list[dict[str, Any]]:
    """Comunidades Leiden (top_n por tamanho) via aggregate no Mongo."""
    return _store.list_communities(repo, top_n=top_n)


@mcp.tool()
def store_info() -> dict[str, Any]:
    """Info do provider Mongo ativo: URI (mascarada), DB, collection, contagens."""
    info = _store.describe()
    info["allow_writes"] = ALLOW_WRITES
    return info


# --------------------------------------------------------------------------
# Tools de ESCRITA (só se MCP_ALLOW_WRITES=true)
# --------------------------------------------------------------------------

def _require_writes() -> None:
    if not ALLOW_WRITES:
        raise RuntimeError(
            "Escrita desabilitada. Setar MCP_ALLOW_WRITES=true para habilitar "
            "index_repo/remove_repo. Em produção, chame MongoStore.publish_repo(url) "
            "diretamente a partir de um job/pipeline externo."
        )


@mcp.tool()
def index_repo(url: str, force: bool = False) -> dict[str, Any]:
    """Clona url efemeramente, extrai grafo com graphify, escreve no MongoDB.

    Disponível apenas com MCP_ALLOW_WRITES=true.
    """
    _require_writes()
    return _store.publish_repo(url, force=force)


@mcp.tool()
def remove_repo(slug: str) -> dict[str, Any]:
    """Remove um repo do store (todos os docs: repo + nodes + links)."""
    _require_writes()
    return _store.remove_repo(slug)


# --------------------------------------------------------------------------

if __name__ == "__main__":
    # log leve sem tocar em I/O de rede
    _safe_uri = (
        "mongodb://***@" + _store.uri.split("@", 1)[1]
        if "@" in _store.uri else _store.uri
    )
    log.info(
        "store = %s | db=%s | collection=%s",
        _safe_uri, _store.db_name, _store.coll_name,
    )
    log.info("allow_writes = %s", ALLOW_WRITES)
    log.info("MCP server pronto (stdio)")
    mcp.run()
