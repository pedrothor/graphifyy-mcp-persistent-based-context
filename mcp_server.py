"""MCP server multi-repo. Storage configurável em settings.py.

Config (env vars ou .env):
    STORAGE_MODE         "local" (montydb+sqlite) ou "remote" (MongoDB). Default: local.
    LOCAL_DATA_DIR       Diretório dos dados no modo local. Default: ./data
    MONGODB_URI          Usado só no modo remote. Default: mongodb://localhost:27017/
    MONGODB_DB           Database name. Default: elos_agent
    MONGODB_COLLECTION   Collection name. Default: mcp
    MCP_ALLOW_WRITES     "true" habilita index_repo/remove_repo. Default: false.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from mongo_store import MongoStore
from settings import settings

ALLOW_WRITES = settings.mcp_allow_writes

# Log em stderr (visível se rodar manual) + arquivo (sempre visível via tail).
# stderr NÃO chega ao usuário quando o cliente MCP auto-spawna o processo,
# por isso o file handler é o meio prático de acompanhar indexações.
_log_dir = Path("logs")
_log_dir.mkdir(exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file_handler = RotatingFileHandler(
    _log_dir / "mcp_server.log",
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler(sys.stderr)
_stream_handler.setFormatter(_fmt)

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(_file_handler)
_root.addHandler(_stream_handler)

log = logging.getLogger("mcp-elos")

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
def list_project_modules() -> list[dict[str, Any]]:
    """Lista os project_module já cadastrados no store, com contagem de repos.

    Use esta tool ANTES de `index_repo` para mostrar ao usuário os módulos
    existentes. O usuário pode escolher um deles ou informar um novo nome.
    Retorna [] se nenhum módulo foi cadastrado ainda — nesse caso peça ao
    usuário pra definir o primeiro.
    """
    return _store.list_project_modules()


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
def index_repo(url: str, project_module: str, force: bool = False) -> dict[str, Any]:
    """Clona url efemeramente, extrai grafo com graphify, escreve no store.

    FLUXO OBRIGATÓRIO antes de chamar esta tool:
      1. Chame `list_project_modules` pra obter os módulos já cadastrados.
      2. Mostre a lista ao usuário e pergunte: "escolhe um destes ou
         digita um novo nome de módulo".
      3. Aguarde a resposta — NÃO invente `project_module`.
      4. Só então chame `index_repo(url, project_module=<resposta>)`.

    Args:
        url: URL do repo (github https ou ssh).
        project_module: nome do módulo (existente ou novo), obrigatório,
            informado pelo usuário.
        force: reindexa mesmo se o SHA remoto for igual ao já indexado.

    Disponível apenas com MCP_ALLOW_WRITES=true.
    """
    _require_writes()
    return _store.publish_repo(url, project_module, force=force)


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
        "mode=%s | store=%s | db=%s | collection=%s",
        _store.mode, _safe_uri, _store.db_name, _store.coll_name,
    )
    log.info("allow_writes = %s", ALLOW_WRITES)
    log.info("MCP server pronto (stdio)")
    mcp.run()
