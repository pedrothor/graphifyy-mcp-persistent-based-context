"""MCP server multi-repo. Storage configurável em settings.py.

Config (env vars ou .env):
    STORAGE_MODE         "local" (montydb+sqlite) ou "remote" (MongoDB). Default: local.
    LOCAL_DATA_DIR       Diretório dos dados no modo local. Default: ./data
    MONGODB_URI          Usado só no modo remote. Default: mongodb://localhost:27017/
    MONGODB_DB           Database name. Default: elos_agent
    MONGODB_COLLECTION   Collection name. Default: mcp
    MCP_ALLOW_WRITES     "true" habilita index_repo/remove_repo. Default: false.
    MCP_TRANSPORT        "stdio" (default) ou "http" (uvicorn em MCP_HOST:MCP_PORT).
    MCP_HOST             Host do servidor HTTP. Default: 127.0.0.1.
    MCP_PORT             Porta do servidor HTTP. Default: 8000.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from mongo_store import MongoStore, list_repo_files as _list_repo_files
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
def list_repos(project: str | None = None) -> list[str]:
    """Slugs de todos os repos presentes no store.

    Se `project` for informado (e DEFAULT_PROJECT não estiver setado no
    .env), filtra por ele. Se DEFAULT_PROJECT estiver setado, sempre
    filtra por esse valor (server travado).
    """
    return _store.list_repo_slugs(project=project)


@mcp.tool()
def list_projects() -> list[dict[str, Any]]:
    """Projetos distintos cadastrados no store (com contagens).

    Retorna [{project, num_repos, num_facts, num_modules}, ...] alfabético.
    Ignora DEFAULT_PROJECT (sempre lista tudo — útil pra o agente descobrir).
    """
    return _store.list_projects()


@mcp.tool()
def list_project_modules(project: str | None = None) -> list[dict[str, Any]]:
    """Lista os project_module já cadastrados no store, com contagem de repos.

    Filtro opcional por `project`. DEFAULT_PROJECT do .env sobrepõe.

    Use esta tool ANTES de `index_repo` para mostrar ao usuário os módulos
    existentes. O usuário pode escolher um deles ou informar um novo nome.
    """
    return _store.list_project_modules(project=project)


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


@mcp.tool()
def list_facts(
    project: str | None = None,
    project_module: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    related_repo: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Lista facts com filtros opcionais (project, project_module, kind, tag, related_repo).

    Fact = conhecimento operacional que não sai de código nem YAML
    (triggers de banco, webhooks, crons, integrações, etc).
    DEFAULT_PROJECT do .env sobrepõe o `project` passado.
    """
    return _store.list_facts(
        project=project, project_module=project_module, kind=kind, tag=tag,
        related_repo=related_repo, limit=limit,
    )


@mcp.tool()
def get_fact(fact_id: str) -> dict[str, Any]:
    """Retorna o fact completo pelo `fact_id` (aceita com ou sem prefixo 'fact:')."""
    doc = _store.get_fact(fact_id)
    if doc is None:
        raise ValueError(f"fact não encontrado: {fact_id}")
    return doc


@mcp.tool()
def search_facts(
    query: str,
    project: str | None = None,
    project_module: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Busca facts por regex (case-insensitive) em title, description e tags.

    Complementa `search_symbol` — este último busca só no grafo de código/infra;
    `search_facts` busca no conhecimento operacional cadastrado à mão.
    DEFAULT_PROJECT do .env sobrepõe o `project` passado.
    """
    return _store.search_facts(
        query, project=project, project_module=project_module, limit=limit,
    )


@mcp.tool()
def list_repo_files(url: str) -> dict[str, Any]:
    """Clona o repo efêmero e devolve inventário de arquivos + detecção.

    NÃO escreve nada no store. Use ANTES de `index_repo` pra decidir se o
    repo tem código (dispara graphify), infra YAML (passa em `infra_files`),
    ou ambos.

    Retorno:
        {
          "status": "ok", "url", "commit_sha", "num_files",
          "files":       [{"path", "ext", "size"}, ...],
          "yaml_files":  [path, ...],    # atalho: só os .yaml/.yml
          "detected":    {"has_code", "has_yaml", "has_terraform"},
        }

    Fluxo recomendado quando o usuário pede pra indexar um repo:
      1. `list_repo_files(url)` — inspeciona
      2. Analise `yaml_files` — separe cicd/k8s/deploy dos que são config da app
      3. `index_repo(url, project_module, infra_files=[apenas os de infra])`
    """
    return _list_repo_files(url)


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
def index_repo(
    url: str,
    project_module: str,
    project: str | None = None,
    force: bool = False,
    infra_files: list[str] | None = None,
) -> dict[str, Any]:
    """Clona url efemeramente, extrai grafo (código + infra opcional), escreve no store.

    FLUXO OBRIGATÓRIO antes de chamar esta tool:
      1. Chame `list_projects` pra ver os projetos existentes.
      2. Chame `list_project_modules(project=...)` pra ver os módulos daquele projeto.
      3. Confirme com o usuário: qual projeto + qual módulo.
      4. Se o repo pode conter YAMLs de infra (k8s, cicd), chame
         `list_repo_files(url)` antes e decida quais entram em `infra_files`.
      5. Só então chame `index_repo(url, project_module=..., project=...,
         infra_files=[...])`.

    Args:
        url: URL do repo (github https ou ssh).
        project_module: nome do módulo (existente ou novo), obrigatório.
        project: projeto de nível superior. Se DEFAULT_PROJECT estiver
            setado no .env, sobrepõe. Se ambos vazios, repo fica orphan.
        force: reindexa mesmo se o SHA remoto for igual ao já indexado.
        infra_files: opcional. Paths (relativos à raiz do repo) de YAMLs de
            infra a serem parseados junto com o código.

    Disponível apenas com MCP_ALLOW_WRITES=true.
    """
    _require_writes()
    return _store.publish_repo(
        url, project_module, project=project, force=force, infra_files=infra_files,
    )


@mcp.tool()
def remove_repo(slug: str) -> dict[str, Any]:
    """Remove um repo do store (todos os docs: repo + nodes + links)."""
    _require_writes()
    return _store.remove_repo(slug)


@mcp.tool()
def add_fact(
    kind: str,
    title: str,
    description: str,
    project_module: str,
    project: str | None = None,
    metadata: dict | None = None,
    related_repos: list[str] | None = None,
    tags: list[str] | None = None,
    fact_id: str | None = None,
) -> dict[str, Any]:
    """Cria ou atualiza (upsert) um fact — conhecimento operacional standalone.

    Se `fact_id` já existe, faz replace preservando `created_at`. Caso contrário
    (ou fact_id=None), gera UUID novo. Retorna o doc final com `fact_id`.

    Args:
        kind: categoria livre (ex: "mongo_trigger", "webhook", "cron", "integration").
        title: título curto (indexado em search_facts).
        description: descrição longa (indexada em search_facts).
        project_module: módulo do projeto ao qual o fact pertence.
        project: projeto de nível superior. DEFAULT_PROJECT do .env sobrepõe.
        metadata: dict livre com payload estruturado por kind (opcional).
        related_repos: slugs de repos relacionados (opcional).
        tags: lista de tags livres (indexadas em search_facts).
        fact_id: se informado, upsert; senão insere com UUID novo.

    Disponível apenas com MCP_ALLOW_WRITES=true.
    """
    _require_writes()
    return _store.add_fact(
        kind=kind, title=title, description=description,
        project_module=project_module, project=project, metadata=metadata,
        related_repos=related_repos, tags=tags, fact_id=fact_id,
    )


@mcp.tool()
def remove_fact(fact_id: str) -> dict[str, Any]:
    """Remove um fact do store."""
    _require_writes()
    return _store.remove_fact(fact_id)


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

    if settings.mcp_transport == "http":
        import uvicorn

        # Endpoint MCP fica em http://host:port/mcp (default do FastMCP)
        log.info(
            "MCP server pronto (http, uvicorn em %s:%d)",
            settings.mcp_host, settings.mcp_port,
        )
        uvicorn.run(
            mcp.streamable_http_app(),
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level="info",
        )
    else:
        log.info("MCP server pronto (stdio)")
        mcp.run()
