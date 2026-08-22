"""MongoStore: único backend de storage do MCP. Guarda repos, nodes e links
como documentos numa única collection.

Modelagem:
    _type = "repo"    -> 1 doc por repo indexado (metadata + summary)
    _type = "node"    -> 1 doc por símbolo do grafo
    _type = "link"    -> 1 doc por aresta

Índices (criados automaticamente):
    (_type, slug)                   listar tudo do repo
    (_type, slug, node_id)          get_node único
    (_type, slug, source)           BFS de vizinhos (out)
    (_type, slug, target)           BFS de vizinhos (in)
    (_type, slug, community)        list_communities
    (_type, label)                  search_symbol cross-repo (regex+i)

Backends (STORAGE_MODE em settings.py):
    "local"   -> montydb (SQLite em LOCAL_DATA_DIR). Não precisa de servidor.
    "remote"  -> MongoDB via MONGODB_URI (pymongo).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

# montydb (backend local) tem sua própria DuplicateKeyError que NÃO herda
# de pymongo. Se instalado, captura as duas para o lock funcionar em ambos
# storage modes (local sqlite e remote MongoDB).
try:
    from montydb.errors import DuplicateKeyError as _MontyDupKey
    _DUP_KEY_ERRORS: tuple = (DuplicateKeyError, _MontyDupKey)
except ImportError:
    _DUP_KEY_ERRORS = (DuplicateKeyError,)

from settings import settings

log = logging.getLogger("mongo_store")

# Sem console/stdin herdados. Necessário quando o processo Python roda como
# filho do MCP client (claude.exe / node.exe) sem console alocado: git.exe e
# subprocessos filhos travam esperando I/O de console herdado, e o
# timeout= do subprocess.run não dispara porque netos seguram os pipes.
_NO_INHERIT: dict = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _NO_INHERIT["creationflags"] = subprocess.CREATE_NO_WINDOW

# Deve cobrir clone+extract do maior repo esperado. Se o processo morrer
# sem release, o lock expira após esse tempo e outro worker pode assumir.
_LOCK_TTL_SECONDS = 600


def _resolve_project(caller_value: str | None) -> str | None:
    """Resolve o project pra writes e filtros de leitura.

    Ordem:
      1. Se settings.default_project setado → sobrepõe (server travado).
      2. Senão, se o caller passou algo não-vazio → usa.
      3. Senão → None (sem escopo / sem filtro).
    """
    default = (settings.default_project or "").strip()
    if default:
        return default
    if caller_value and str(caller_value).strip():
        return str(caller_value).strip()
    return None


class MongoStore:
    def __init__(
        self,
        uri: str | None = None,
        db_name: str | None = None,
        collection_name: str | None = None,
        mode: str | None = None,
    ) -> None:
        self.mode = mode or settings.storage_mode
        self.db_name = db_name or settings.mongodb_db
        self.coll_name = collection_name or settings.mongodb_collection

        if self.mode == "local":
            from montydb import MontyClient, set_storage

            data_dir = settings.local_data_dir.expanduser().resolve()
            data_dir.mkdir(parents=True, exist_ok=True)
            # set_storage é idempotente; usa SQLite pra durabilidade/queries
            set_storage(
                repository=str(data_dir),
                storage="sqlite",
                use_bson=False,
            )
            self.uri = f"montydb+sqlite://{data_dir}"
            self._client = MontyClient(str(data_dir))
            self._db = self._client[self.db_name]
            # sqlite backend não auto-cria a collection em delete/find;
            # cria uma vez pra todas as ops funcionarem.
            if self.coll_name not in self._db.list_collection_names():
                self._db.create_collection(self.coll_name)
        else:
            from pymongo import MongoClient

            self.uri = uri or settings.mongodb_uri
            # MongoClient é lazy: não conecta até a primeira operação
            self._client = MongoClient(self.uri)
            self._db = self._client[self.db_name]

        self._coll = self._db[self.coll_name]
        self._indexes_ensured = False

    @property
    def coll(self) -> Any:
        """Garante índices na primeira operação real (lazy).

        Retorna pymongo.collection.Collection (remote) ou
        montydb.collection.MontyCollection (local) — mesma API para as
        operações que usamos.
        """
        if not self._indexes_ensured:
            self._ensure_indexes()
            self._indexes_ensured = True
        return self._coll

    # ----------------------------------------------------------------------
    # setup
    # ----------------------------------------------------------------------

    def _ensure_indexes(self) -> None:
        """Idempotente: cria só se não existir. Chamado lazy."""
        self._coll.create_index([("_type", ASCENDING), ("slug", ASCENDING)], name="type_slug")
        self._coll.create_index(
            [("_type", ASCENDING), ("slug", ASCENDING), ("node_id", ASCENDING)],
            name="type_slug_nodeid",
        )
        self._coll.create_index(
            [("_type", ASCENDING), ("slug", ASCENDING), ("source", ASCENDING)],
            name="type_slug_source",
        )
        self._coll.create_index(
            [("_type", ASCENDING), ("slug", ASCENDING), ("target", ASCENDING)],
            name="type_slug_target",
        )
        self._coll.create_index(
            [("_type", ASCENDING), ("slug", ASCENDING), ("community", ASCENDING)],
            name="type_slug_community",
        )
        self._coll.create_index([("_type", ASCENDING), ("label", ASCENDING)], name="type_label")

    def describe(self) -> dict[str, Any]:
        # esconde credencial da URI
        safe_uri = self.uri
        if "@" in safe_uri:
            safe_uri = "mongodb://***@" + safe_uri.split("@", 1)[1]
        info: dict[str, Any] = {
            "kind": "mongodb",
            "uri": safe_uri,
            "database": self.db_name,
            "collection": self.coll_name,
        }
        # counts requerem conexão; se offline, marca como unavailable
        try:
            info["total_documents"] = self._coll.count_documents({})
            info["repos"] = self._coll.count_documents({"_type": "repo"})
            info["connected"] = True
        except Exception as e:
            info["connected"] = False
            info["error"] = str(e)[:200]
        return info

    # ----------------------------------------------------------------------
    # LEITURA — usada pelas tools do MCP
    # ----------------------------------------------------------------------

    def list_repo_slugs(self, project: str | None = None) -> list[str]:
        query: dict[str, Any] = {"_type": "repo"}
        resolved = _resolve_project(project)
        if resolved:
            query["project"] = resolved
        return sorted(
            doc["slug"] for doc in self.coll.find(query, {"slug": 1, "_id": 0})
        )

    def list_project_modules(self, project: str | None = None) -> list[dict[str, Any]]:
        """Módulos distintos já cadastrados, com contagem de repos e slugs.

        Se `project` (resolvido via .env override) for informado, filtra
        só módulos daquele projeto.

        Retorna [{module, num_repos, repos: [slug,...]}, ...] ordenado por
        num_repos desc.
        """
        query: dict[str, Any] = {"_type": "repo"}
        resolved = _resolve_project(project)
        if resolved:
            query["project"] = resolved
        buckets: dict[str, list[str]] = defaultdict(list)
        for doc in self.coll.find(
            query, {"slug": 1, "project_module": 1, "_id": 0}
        ):
            mod = doc.get("project_module")
            if mod:
                buckets[mod].append(doc["slug"])
        return [
            {"module": mod, "num_repos": len(slugs), "repos": sorted(slugs)}
            for mod, slugs in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
        ]

    def list_projects(self) -> list[dict[str, Any]]:
        """Projetos distintos cadastrados (union de repos + facts).

        Retorna [{project, num_repos, num_facts, num_modules}, ...] ordenado
        alfabeticamente. Sempre lista tudo — não aplica DEFAULT_PROJECT
        override (senão veria só o próprio projeto).
        """
        stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"num_repos": 0, "num_facts": 0, "modules": set()}
        )
        for doc in self.coll.find(
            {"_type": "repo", "project": {"$ne": None}},
            {"project": 1, "project_module": 1, "_id": 0},
        ):
            p = doc.get("project")
            if not p:
                continue
            stats[p]["num_repos"] += 1
            mod = doc.get("project_module")
            if mod:
                stats[p]["modules"].add(mod)
        for doc in self.coll.find(
            {"_type": "fact", "project": {"$ne": None}},
            {"project": 1, "_id": 0},
        ):
            p = doc.get("project")
            if not p:
                continue
            stats[p]["num_facts"] += 1
        return [
            {
                "project": p,
                "num_repos": s["num_repos"],
                "num_facts": s["num_facts"],
                "num_modules": len(s["modules"]),
            }
            for p, s in sorted(stats.items())
        ]

    def get_index(self) -> dict[str, Any]:
        """Retorna um "index" no mesmo formato dos providers antigos.

        Inclui todos os campos summary do repo (top_hubs, top_communities, etc)
        mas SEM os nodes/links completos — busca de símbolos usa Mongo direto.
        """
        repos: dict[str, Any] = {}
        for doc in self.coll.find({"_type": "repo"}, {"_id": 0}):
            slug = doc.pop("slug", None)
            if slug:
                doc.pop("_type", None)
                repos[slug] = doc
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "repos": repos,
        }

    def get_repo_summary(self, slug: str) -> dict[str, Any] | None:
        doc = self.coll.find_one({"_type": "repo", "slug": slug}, {"_id": 0, "_type": 0})
        return doc

    def get_node(self, slug: str, node_id: str) -> dict[str, Any] | None:
        # tenta por node_id exato primeiro
        doc = self.coll.find_one(
            {"_type": "node", "slug": slug, "node_id": node_id},
            {"_id": 0, "_type": 0},
        )
        if doc:
            return _rename_node_id(doc)
        # fallback: por label (case-insensitive exato)
        doc = self.coll.find_one(
            {"_type": "node", "slug": slug, "label": {"$regex": f"^{re.escape(node_id)}$", "$options": "i"}},
            {"_id": 0, "_type": 0},
        )
        return _rename_node_id(doc) if doc else None

    def search_symbol(
        self, pattern: str, slug: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"_type": "node"}
        if slug:
            query["slug"] = slug
        rx = {"$regex": re.escape(pattern), "$options": "i"}
        query["$or"] = [{"label": rx}, {"node_id": rx}]
        results = []
        for doc in self.coll.find(query, {"_id": 0, "_type": 0}).limit(limit):
            doc = _rename_node_id(doc)
            results.append(doc)
        return results

    def get_neighbors(self, slug: str, node_id: str, depth: int = 1) -> dict[str, Any]:
        # resolve node de partida (aceita id ou label)
        start = self.get_node(slug, node_id)
        if start is None:
            raise ValueError(f"node não encontrado: {node_id}")
        root_id = start.get("id")

        visited: set[str] = {root_id}
        frontier: set[str] = {root_id}
        for _ in range(max(1, depth)):
            if not frontier:
                break
            nxt: set[str] = set()
            # queries em uma só rodada com $in
            frontier_list = list(frontier)
            for link in self.coll.find(
                {
                    "_type": "link",
                    "slug": slug,
                    "$or": [{"source": {"$in": frontier_list}}, {"target": {"$in": frontier_list}}],
                },
                {"source": 1, "target": 1, "_id": 0},
            ):
                for n in (link["source"], link["target"]):
                    if n not in visited:
                        nxt.add(n)
            visited |= nxt
            frontier = nxt

        # busca todos os nodes visitados
        node_docs = list(
            self.coll.find(
                {"_type": "node", "slug": slug, "node_id": {"$in": list(visited)}},
                {"_id": 0, "_type": 0, "slug": 0},
            )
        )
        nodes = [_rename_node_id(n) for n in node_docs]
        # e todas as arestas entre eles
        edges = list(
            self.coll.find(
                {
                    "_type": "link",
                    "slug": slug,
                    "source": {"$in": list(visited)},
                    "target": {"$in": list(visited)},
                },
                {"_id": 0, "_type": 0, "slug": 0},
            )
        )
        return {"root": root_id, "depth": depth, "nodes": nodes, "edges": edges}

    def shortest_path(self, slug: str, source: str, target: str) -> dict[str, Any]:
        s = self.get_node(slug, source)
        t = self.get_node(slug, target)
        if s is None or t is None:
            raise ValueError("source ou target não encontrado")
        sid, tid = s["id"], t["id"]
        if sid == tid:
            return {"source": sid, "target": tid, "length": 0, "path": [sid]}

        # BFS bidirecional na aresta (undirected)
        parent: dict[str, str | None] = {sid: None}
        frontier: set[str] = {sid}
        found = False
        for _ in range(20):  # limite prático
            if not frontier or found:
                break
            frontier_list = list(frontier)
            nxt: set[str] = set()
            for link in self.coll.find(
                {
                    "_type": "link",
                    "slug": slug,
                    "$or": [{"source": {"$in": frontier_list}}, {"target": {"$in": frontier_list}}],
                },
                {"source": 1, "target": 1, "_id": 0},
            ):
                for a, b in ((link["source"], link["target"]), (link["target"], link["source"])):
                    if a in frontier and b not in parent:
                        parent[b] = a
                        nxt.add(b)
                        if b == tid:
                            found = True
                            break
                if found:
                    break
            frontier = nxt
            if tid in parent:
                found = True
                break

        if tid not in parent:
            return {"source": sid, "target": tid, "path": None, "reason": "sem caminho"}
        path = []
        cur: str | None = tid
        while cur is not None:
            path.append(cur)
            cur = parent[cur]
        path.reverse()
        return {"source": sid, "target": tid, "length": len(path) - 1, "path": path}

    def list_communities(self, slug: str, top_n: int = 20) -> list[dict[str, Any]]:
        # find + agrupamento em Python (montydb não implementa aggregate).
        # Volume por repo é pequeno (nodes), então custo é aceitável.
        buckets: dict[Any, list[str | None]] = defaultdict(list)
        for doc in self.coll.find(
            {"_type": "node", "slug": slug, "community": {"$ne": None}},
            {"community": 1, "label": 1, "_id": 0},
        ):
            buckets[doc["community"]].append(doc.get("label"))
        top = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[:top_n]
        return [
            {"community_id": cid, "size": len(labels), "sample_labels": labels[:8]}
            for cid, labels in top
        ]

    # ----------------------------------------------------------------------
    # ESCRITA — usada pela tool index_repo do MCP e por qualquer código
    # externo que queira publicar/atualizar um repo no store.
    # ----------------------------------------------------------------------

    def publish_repo(
        self,
        url: str,
        project_module: str,
        *,
        project: str | None = None,
        force: bool = False,
        graphify_bin: str | None = None,
        infra_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Clona o url, extrai o grafo com graphify, escreve no store.

        Args:
            url: URL do repo (github https ou ssh).
            project_module: módulo do projeto ao qual o repo pertence
                (ex.: "payments", "billing"). Obrigatório — usado pra
                agrupar repos correlatos no cross-repo search.
            project: projeto de nível superior. Se DEFAULT_PROJECT estiver
                setado no .env, sobrepõe este valor. Se ambos vazios, o
                repo fica sem project (orphan).
            infra_files: opcional. Lista de paths (relativos à raiz do repo)
                de arquivos YAML de infra a serem parseados e mergeados no
                grafo. Ex.: ["cicd/k8s.dev.yaml"]. Se None, nada de infra é
                indexado. Se a lista está vazia, é tratada como None.

        Retorna dict com status e stats. Em `stats.infra` vem contagem por
        origem (ex.: {"infra_k8s": {"nodes": 3, "links": 2}}).
        """
        if not project_module or not project_module.strip():
            raise ValueError("project_module é obrigatório")
        project_module = project_module.strip()
        resolved_project = _resolve_project(project)
        # imports locais para não puxar dependências quando o server for read-only
        from ingest import parse_github_url, remote_head_sha, repo_slug, GRAPHIFY_BIN, git_env

        graphify_bin = graphify_bin or GRAPHIFY_BIN
        infra_files = [p for p in (infra_files or []) if p and p.strip()] or None

        owner, repo = parse_github_url(url)
        slug = repo_slug(owner, repo)

        # Skip-check só faz sentido quando já existe registro (temos SHA pra comparar).
        # Isso evita um `git ls-remote` inútil na primeira indexação — e ls-remote
        # sem timeout foi a causa raiz do hang de 30min anterior.
        existing = self.get_repo_summary(slug) if not force else None
        if existing:
            t0 = time.monotonic()
            log.info("[%s] checando SHA remoto (timeout=20s)", slug)
            remote_sha = remote_head_sha(url, timeout=20.0)
            log.info("[%s] remote sha=%s (em %.2fs)",
                     slug, (remote_sha[:8] if remote_sha else "unknown"), time.monotonic() - t0)
            if remote_sha and existing.get("commit_sha") == remote_sha:
                log.info("[%s] SHA %s já indexado, pulando", slug, remote_sha[:8])
                return {"status": "skipped", "slug": slug,
                        "reason": "SHA remoto igual ao já processado"}
        else:
            log.info("[%s] repo novo (ou force=True), pulando ls-remote", slug)

        # Lock por slug: impede duas indexações concorrentes do mesmo repo.
        # Usa unicidade do _id como primitiva atômica (funciona em Mongo real
        # e em montydb+sqlite). TTL manual pra sobreviver a workers mortos.
        lock_id = f"lock:{slug}"
        now = datetime.now(timezone.utc)
        lock_doc = {
            "_id": lock_id,
            "_type": "lock",
            "slug": slug,
            "acquired_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=_LOCK_TTL_SECONDS)).isoformat(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }
        try:
            self.coll.insert_one(lock_doc)
        except _DUP_KEY_ERRORS:
            existing_lock = self.coll.find_one({"_id": lock_id}) or {}
            exp = existing_lock.get("expires_at", "")
            if exp and exp < now.isoformat():
                log.warning("[%s] lock órfão (expirou em %s), assumindo", slug, exp)
                self.coll.delete_one({"_id": lock_id})
                try:
                    self.coll.insert_one(lock_doc)
                except _DUP_KEY_ERRORS:
                    return {"status": "already_running", "slug": slug,
                            "reason": "outro worker adquiriu o lock após steal"}
            else:
                log.info("[%s] já em andamento por %s/pid=%s (desde %s)",
                         slug, existing_lock.get("host"), existing_lock.get("pid"),
                         existing_lock.get("acquired_at"))
                return {
                    "status": "already_running",
                    "slug": slug,
                    "acquired_at": existing_lock.get("acquired_at"),
                    "expires_at": existing_lock.get("expires_at"),
                    "host": existing_lock.get("host"),
                    "pid": existing_lock.get("pid"),
                }
        log.info("[%s] lock adquirido (ttl=%ds)", slug, _LOCK_TTL_SECONDS)

        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="mongo_publish_"))
            log.info("[%s] clonando em %s (efêmero, timeout=120s)", slug, tmp_dir)
            try:
                t0 = time.monotonic()
                try:
                    subprocess.run(
                        ["git", "clone", "--depth", "1", url, str(tmp_dir)],
                        capture_output=True, text=True, check=True,
                        timeout=120, env=git_env(),
                        **_NO_INHERIT,
                    )
                except subprocess.TimeoutExpired:
                    return {"status": "failed", "slug": slug,
                            "reason": "timeout: git clone excedeu 120s"}
                except subprocess.CalledProcessError as e:
                    return {"status": "failed", "slug": slug,
                            "reason": f"git clone rc={e.returncode}",
                            "stderr": (e.stderr or "")[:500]}
                log.info("[%s] clone OK (%.2fs)", slug, time.monotonic() - t0)

                try:
                    head = subprocess.run(
                        ["git", "-C", str(tmp_dir), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True,
                        timeout=5, env=git_env(),
                        **_NO_INHERIT,
                    )
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
                    return {"status": "failed", "slug": slug,
                            "reason": f"git rev-parse falhou: {type(e).__name__}"}
                clone_sha = head.stdout.strip()

                t0 = time.monotonic()
                log.info("[%s] rodando graphify extract --code-only (timeout=300s)", slug)
                out_dir = tmp_dir / "_out"
                try:
                    result = subprocess.run(
                        [graphify_bin, "extract", str(tmp_dir), "--code-only", "--out", str(out_dir)],
                        capture_output=True, text=True, timeout=300,
                        **_NO_INHERIT,
                    )
                except subprocess.TimeoutExpired:
                    return {"status": "failed", "slug": slug,
                            "reason": "timeout: graphify extract excedeu 300s"}
                log.info("[%s] graphify done (%.2fs, rc=%s)",
                         slug, time.monotonic() - t0, result.returncode)

                nodes: list[dict] = []
                links: list[dict] = []
                code_ok = False
                if result.returncode == 0:
                    graph_json = out_dir / "graphify-out" / "graph.json"
                    if graph_json.exists():
                        import json
                        with graph_json.open(encoding="utf-8") as f:
                            graph = json.load(f)
                        nodes = list(graph.get("nodes") or [])
                        links = list(graph.get("links") or [])
                        code_ok = True
                    else:
                        log.warning("[%s] graphify rc=0 mas graph.json ausente", slug)
                else:
                    log.warning("[%s] graphify rc=%s: %s",
                                slug, result.returncode, (result.stderr or "")[:200])

                # Se não temos código nem infra pra indexar, aborta com erro claro.
                if not code_ok and not infra_files:
                    return {"status": "failed", "slug": slug,
                            "reason": f"graphify extract falhou (rc={result.returncode}) "
                                      "e nenhum infra_files foi passado",
                            "stderr": (result.stderr or "")[:500]}

                infra_stats: dict[str, dict] = {}
                if infra_files:
                    import infra_extract
                    resolved: list[Path] = []
                    skipped: list[str] = []
                    for rel_path in infra_files:
                        # sanitize: precisa estar dentro do tmp_dir
                        candidate = (tmp_dir / rel_path).resolve()
                        try:
                            candidate.relative_to(tmp_dir.resolve())
                        except ValueError:
                            skipped.append(f"{rel_path} (fora do repo)")
                            continue
                        if not candidate.exists():
                            skipped.append(f"{rel_path} (não existe)")
                            continue
                        resolved.append(candidate)
                    if skipped:
                        log.warning("[%s] infra_files ignorados: %s", slug, skipped)

                    t0 = time.monotonic()
                    log.info("[%s] parseando %d arquivo(s) de infra (K8s)", slug, len(resolved))
                    k8s_nodes, k8s_links = infra_extract.extract_k8s(resolved, tmp_dir)
                    log.info("[%s] infra K8s: %d nodes, %d links em %.2fs",
                             slug, len(k8s_nodes), len(k8s_links), time.monotonic() - t0)
                    nodes.extend(k8s_nodes)
                    links.extend(k8s_links)
                    infra_stats["infra_k8s"] = {
                        "nodes": len(k8s_nodes),
                        "links": len(k8s_links),
                        "files_processed": len(resolved),
                        "files_skipped": skipped,
                    }

                t0 = time.monotonic()
                log.info("[%s] escrevendo no store (%d nodes total, %d links total)",
                         slug, len(nodes), len(links))
                self._write_repo(slug, url, clone_sha, project_module, resolved_project, nodes, links)
                log.info("[%s] store write OK (%.2fs)", slug, time.monotonic() - t0)

                return {
                    "status": "extracted",
                    "slug": slug,
                    "url": url,
                    "commit_sha": clone_sha,
                    "project": resolved_project,
                    "project_module": project_module,
                    "num_nodes": len(nodes),
                    "num_links": len(links),
                    "code_extracted": code_ok,
                    "infra": infra_stats,
                }
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                log.info("[%s] clone efêmero deletado", slug)
        finally:
            self.coll.delete_one({"_id": lock_id})
            log.info("[%s] lock liberado", slug)

    def _write_repo(
        self,
        slug: str,
        url: str,
        commit_sha: str,
        project_module: str,
        project: str | None,
        nodes: list[dict],
        links: list[dict],
    ) -> None:
        """Escrita atômica: apaga tudo desse slug + insere de novo (upsert bulk)."""
        # 1. delete old
        self.coll.delete_many({"slug": slug})

        # 2. bulk insert nodes
        if nodes:
            node_docs = []
            for n in nodes:
                nid = n.get("id")
                if nid is None:
                    continue
                doc = {
                    "_id": f"node:{slug}:{nid}",
                    "_type": "node",
                    "slug": slug,
                    "node_id": nid,
                    "label": n.get("label"),
                    "norm_label": n.get("norm_label"),
                    "source_file": n.get("source_file"),
                    "source_location": n.get("source_location"),
                    "file_type": n.get("file_type"),
                    "community": n.get("community"),
                    "_origin": n.get("_origin"),
                }
                # inclui campos extras que porventura o graphify tenha adicionado
                for k, v in n.items():
                    if k not in doc and k != "id":
                        doc[k] = v
                node_docs.append(doc)
            if node_docs:
                self.coll.insert_many(node_docs, ordered=False)

        # 3. bulk insert links
        if links:
            link_docs = []
            for i, l in enumerate(links):
                src, tgt = l.get("source"), l.get("target")
                if src is None or tgt is None:
                    continue
                doc = {
                    "_id": f"link:{slug}:{i}",
                    "_type": "link",
                    "slug": slug,
                    "source": src,
                    "target": tgt,
                    "relation": l.get("relation"),
                    "confidence": l.get("confidence"),
                    "confidence_score": l.get("confidence_score"),
                    "source_file": l.get("source_file"),
                    "source_location": l.get("source_location"),
                    "weight": l.get("weight"),
                    "_origin": l.get("_origin"),
                }
                for k, v in l.items():
                    if k not in doc and k not in ("source", "target"):
                        doc[k] = v
                link_docs.append(doc)
            if link_docs:
                self.coll.insert_many(link_docs, ordered=False)

        # 4. compute repo summary (top hubs + top communities + file_types)
        summary = _compute_summary(slug, url, commit_sha, project_module, project, nodes, links)
        summary["_id"] = f"repo:{slug}"
        summary["_type"] = "repo"
        self.coll.replace_one({"_id": summary["_id"]}, summary, upsert=True)

    def remove_repo(self, slug: str) -> dict[str, Any]:
        result = self.coll.delete_many({"slug": slug})
        return {"status": "removed", "slug": slug, "deleted_documents": result.deleted_count}

    # ----------------------------------------------------------------------
    # FACTS — conhecimento operacional que não sai de código nem YAML
    # (triggers, webhooks, crons, integrações, etc). Docs standalone,
    # sem espelho no grafo (Opção A). Buscas via search_facts/list_facts.
    # ----------------------------------------------------------------------

    def add_fact(
        self,
        kind: str,
        title: str,
        description: str,
        project_module: str,
        *,
        project: str | None = None,
        metadata: dict | None = None,
        related_repos: list[str] | None = None,
        tags: list[str] | None = None,
        fact_id: str | None = None,
    ) -> dict[str, Any]:
        """Cria ou atualiza (upsert) um fact.

        Se `fact_id` foi passado e já existe, faz replace preservando o
        `created_at` original. Se não existe (ou fact_id=None), gera um
        UUID novo e insere.

        `project` segue a mesma resolução do publish_repo: DEFAULT_PROJECT
        do .env sobrepõe; senão usa o que o caller passou; senão fica None.

        Args obrigatórios: kind, title, description, project_module.
        """
        for field, val in (("kind", kind), ("title", title),
                           ("description", description),
                           ("project_module", project_module)):
            if not val or not str(val).strip():
                raise ValueError(f"{field} é obrigatório")

        fid = (fact_id or "").strip() or str(uuid.uuid4())
        # normaliza: aceita "fact:<uuid>" ou só "<uuid>"
        if fid.startswith("fact:"):
            fid = fid[len("fact:"):]
        doc_id = f"fact:{fid}"

        now = datetime.now(timezone.utc).isoformat()
        existing = self.coll.find_one({"_id": doc_id})
        created_at = existing["created_at"] if existing else now

        doc: dict[str, Any] = {
            "_id": doc_id,
            "_type": "fact",
            "kind": str(kind).strip(),
            "title": str(title).strip(),
            "description": str(description).strip(),
            "project": _resolve_project(project),
            "project_module": str(project_module).strip(),
            "metadata": dict(metadata or {}),
            "related_repos": list(related_repos or []),
            "tags": list(tags or []),
            "created_at": created_at,
            "updated_at": now,
        }
        self.coll.replace_one({"_id": doc_id}, doc, upsert=True)
        return _fact_out(doc.copy())

    def remove_fact(self, fact_id: str) -> dict[str, Any]:
        fid = fact_id.strip()
        if fid.startswith("fact:"):
            fid = fid[len("fact:"):]
        doc_id = f"fact:{fid}"
        result = self.coll.delete_one({"_id": doc_id})
        return {"status": "removed" if result.deleted_count else "not_found",
                "fact_id": fid, "deleted_documents": result.deleted_count}

    def get_fact(self, fact_id: str) -> dict[str, Any] | None:
        fid = fact_id.strip()
        if fid.startswith("fact:"):
            fid = fid[len("fact:"):]
        doc = self.coll.find_one({"_id": f"fact:{fid}"})
        return _fact_out(doc)

    def list_facts(
        self,
        *,
        project: str | None = None,
        project_module: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
        related_repo: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"_type": "fact"}
        resolved = _resolve_project(project)
        if resolved:
            query["project"] = resolved
        if project_module:
            query["project_module"] = project_module
        if kind:
            query["kind"] = kind
        if tag:
            query["tags"] = tag
        if related_repo:
            query["related_repos"] = related_repo
        cursor = self.coll.find(query).limit(limit)
        return [_fact_out(d) for d in cursor if d is not None]

    def search_facts(
        self,
        pattern: str,
        *,
        project: str | None = None,
        project_module: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rx = {"$regex": re.escape(pattern), "$options": "i"}
        query: dict[str, Any] = {
            "_type": "fact",
            "$or": [{"title": rx}, {"description": rx}, {"tags": rx}],
        }
        resolved = _resolve_project(project)
        if resolved:
            query["project"] = resolved
        if project_module:
            query["project_module"] = project_module
        cursor = self.coll.find(query).limit(limit)
        return [_fact_out(d) for d in cursor if d is not None]

    def close(self) -> None:
        self._client.close()


def list_repo_files(url: str) -> dict[str, Any]:
    """Clona o repo efêmero e devolve inventário de arquivos + flags de detecção.

    Não escreve nada no store. Pensado pra LLM inspecionar o conteúdo antes
    de decidir se chama index_repo com/sem `infra_files=[...]`.

    Retorna:
        {
          "url": str, "commit_sha": str,
          "num_files": int,
          "files": [{"path", "ext", "size"}, ...],
          "yaml_files": [path, ...],
          "detected": {"has_code", "has_yaml", "has_terraform"},
        }
    """
    from ingest import parse_github_url, git_env
    import infra_extract

    parse_github_url(url)  # valida URL cedo
    tmp_dir = Path(tempfile.mkdtemp(prefix="mongo_list_files_"))
    log.info("[list_repo_files] clonando %s em %s (timeout=120s)", url, tmp_dir)
    try:
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(tmp_dir)],
                capture_output=True, text=True, check=True,
                timeout=120, env=git_env(),
                **_NO_INHERIT,
            )
        except subprocess.TimeoutExpired:
            return {"status": "failed", "url": url,
                    "reason": "timeout: git clone excedeu 120s"}
        except subprocess.CalledProcessError as e:
            return {"status": "failed", "url": url,
                    "reason": f"git clone rc={e.returncode}",
                    "stderr": (e.stderr or "")[:500]}

        try:
            head = subprocess.run(
                ["git", "-C", str(tmp_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
                timeout=5, env=git_env(),
                **_NO_INHERIT,
            )
            commit_sha = head.stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            commit_sha = ""

        inventory = infra_extract.classify_files(tmp_dir)
        return {
            "status": "ok",
            "url": url,
            "commit_sha": commit_sha,
            **inventory,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("[list_repo_files] clone efêmero deletado")


# ----- helpers ---------------------------------------------------------------

def _rename_node_id(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Documento no Mongo usa `node_id`; formato de saída usa `id`."""
    if doc is None:
        return None
    if "node_id" in doc:
        doc["id"] = doc.pop("node_id")
    return doc


def _fact_out(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip `_id`/`_type` internos e expõe `fact_id` (só o UUID)."""
    if doc is None:
        return None
    _id = str(doc.pop("_id", ""))
    doc.pop("_type", None)
    if _id.startswith("fact:"):
        doc["fact_id"] = _id[len("fact:"):]
    return doc


def _compute_summary(
    slug: str,
    url: str,
    commit_sha: str,
    project_module: str,
    project: str | None,
    nodes: list[dict],
    links: list[dict],
) -> dict[str, Any]:
    # graus por nó
    degree: dict[str, int] = defaultdict(int)
    for l in links:
        for k in ("source", "target"):
            v = l.get(k)
            if v:
                degree[v] += 1
    by_id = {n.get("id"): n for n in nodes}

    def _label(n: dict) -> str:
        return str(n.get("label") or n.get("norm_label") or n.get("id") or "")

    top_hubs = []
    for nid, deg in sorted(degree.items(), key=lambda x: x[1], reverse=True)[:5]:
        node = by_id.get(nid, {})
        top_hubs.append({
            "id": nid, "label": _label(node),
            "degree": deg, "source_file": node.get("source_file"),
        })

    communities: dict[Any, list[dict]] = defaultdict(list)
    for n in nodes:
        c = n.get("community")
        if c is not None:
            communities[c].append(n)
    top_communities = []
    for cid, members in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        top_communities.append({
            "id": cid, "size": len(members),
            "sample": [_label(m) for m in members[:5]],
        })

    file_types = Counter(n.get("file_type", "unknown") for n in nodes)

    return {
        "slug": slug,
        "url": url,
        "commit_sha": commit_sha,
        "project": project,
        "project_module": project_module,
        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "num_nodes": len(nodes),
        "num_links": len(links),
        "num_communities": len(communities),
        "file_types": dict(file_types),
        "top_hubs": top_hubs,
        "top_communities": top_communities,
    }
