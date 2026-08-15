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
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import ASCENDING

from settings import settings

log = logging.getLogger("mongo_store")

# Sem console/stdin herdados. Necessário quando o processo Python roda como
# filho do MCP client (claude.exe / node.exe) sem console alocado: git.exe e
# subprocessos filhos travam esperando I/O de console herdado, e o
# timeout= do subprocess.run não dispara porque netos seguram os pipes.
_NO_INHERIT: dict = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    _NO_INHERIT["creationflags"] = subprocess.CREATE_NO_WINDOW


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

    def list_repo_slugs(self) -> list[str]:
        return sorted(
            doc["slug"] for doc in self.coll.find({"_type": "repo"}, {"slug": 1, "_id": 0})
        )

    def list_project_modules(self) -> list[dict[str, Any]]:
        """Módulos distintos já cadastrados, com contagem de repos e slugs.

        Retorna [{module, num_repos, repos: [slug,...]}, ...] ordenado por
        num_repos desc.
        """
        buckets: dict[str, list[str]] = defaultdict(list)
        for doc in self.coll.find(
            {"_type": "repo"}, {"slug": 1, "project_module": 1, "_id": 0}
        ):
            mod = doc.get("project_module")
            if mod:
                buckets[mod].append(doc["slug"])
        return [
            {"module": mod, "num_repos": len(slugs), "repos": sorted(slugs)}
            for mod, slugs in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
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
        force: bool = False,
        graphify_bin: str | None = None,
    ) -> dict[str, Any]:
        """Clona o url, extrai o grafo com graphify, escreve no store.

        Args:
            url: URL do repo (github https ou ssh).
            project_module: módulo do projeto ao qual o repo pertence
                (ex.: "payments", "billing"). Obrigatório — usado pra
                agrupar repos correlatos no cross-repo search.

        Retorna dict com status e stats.
        """
        if not project_module or not project_module.strip():
            raise ValueError("project_module é obrigatório")
        project_module = project_module.strip()
        # imports locais para não puxar dependências quando o server for read-only
        from ingest import parse_github_url, remote_head_sha, repo_slug, GRAPHIFY_BIN, git_env

        graphify_bin = graphify_bin or GRAPHIFY_BIN

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
            if result.returncode != 0:
                return {
                    "status": "failed",
                    "slug": slug,
                    "reason": f"graphify extract rc={result.returncode}",
                    "stderr": (result.stderr or "")[:500],
                }

            graph_json = out_dir / "graphify-out" / "graph.json"
            if not graph_json.exists():
                return {"status": "failed", "slug": slug, "reason": "graph.json não gerado"}

            import json
            with graph_json.open(encoding="utf-8") as f:
                graph = json.load(f)

            nodes = graph.get("nodes", [])
            links = graph.get("links", [])

            t0 = time.monotonic()
            log.info("[%s] escrevendo no store (%d nodes, %d links)",
                     slug, len(nodes), len(links))
            self._write_repo(slug, url, clone_sha, project_module, nodes, links)
            log.info("[%s] store write OK (%.2fs)", slug, time.monotonic() - t0)

            return {
                "status": "extracted",
                "slug": slug,
                "url": url,
                "commit_sha": clone_sha,
                "project_module": project_module,
                "num_nodes": len(nodes),
                "num_links": len(links),
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.info("[%s] clone efêmero deletado", slug)

    def _write_repo(
        self,
        slug: str,
        url: str,
        commit_sha: str,
        project_module: str,
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
        summary = _compute_summary(slug, url, commit_sha, project_module, nodes, links)
        summary["_id"] = f"repo:{slug}"
        summary["_type"] = "repo"
        self.coll.replace_one({"_id": summary["_id"]}, summary, upsert=True)

    def remove_repo(self, slug: str) -> dict[str, Any]:
        result = self.coll.delete_many({"slug": slug})
        return {"status": "removed", "slug": slug, "deleted_documents": result.deleted_count}

    def close(self) -> None:
        self._client.close()


# ----- helpers ---------------------------------------------------------------

def _rename_node_id(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Documento no Mongo usa `node_id`; formato de saída usa `id`."""
    if doc is None:
        return None
    if "node_id" in doc:
        doc["id"] = doc.pop("node_id")
    return doc


def _compute_summary(
    slug: str,
    url: str,
    commit_sha: str,
    project_module: str,
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
        "project_module": project_module,
        "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "num_nodes": len(nodes),
        "num_links": len(links),
        "num_communities": len(communities),
        "file_types": dict(file_types),
        "top_hubs": top_hubs,
        "top_communities": top_communities,
    }
