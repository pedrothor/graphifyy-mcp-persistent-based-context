"""Gera index.json a partir dos grafos comprimidos em um store dir.

Uso CLI:
    python build_index.py                    # usa REPOS_DIR do ingest.py
    python build_index.py <store_dir>        # usa dir arbitrário

Uso programático (reusado pelo mcp_server.py):
    from build_index import build_index
    idx = build_index(store_dir)   # também grava store_dir/index.json
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("build_index")


def _load_graph_gz(graph_gz: Path) -> dict[str, Any]:
    with gzip.open(graph_gz, "rt", encoding="utf-8") as f:
        return json.load(f)


def _repo_summary(slug: str, repo_dir: Path) -> dict[str, Any] | None:
    """Constrói o summary de um repo indexado. Retorna None se dados inválidos."""
    graph_gz = repo_dir / "graphify-out" / "graph.json.gz"
    meta_path = repo_dir / "meta.json"
    if not graph_gz.exists():
        return None

    data = _load_graph_gz(graph_gz)
    nodes = data.get("nodes", [])
    links = data.get("links", [])

    # grau por nó (indeg + outdeg)
    degree: dict[str, int] = defaultdict(int)
    for link in links:
        src, tgt = link.get("source"), link.get("target")
        if src: degree[src] += 1
        if tgt: degree[tgt] += 1

    by_id = {n.get("id"): n for n in nodes}

    def _node_name(n: dict) -> str:
        return str(n.get("label") or n.get("norm_label") or n.get("id") or "")

    top_hubs = []
    for nid, deg in sorted(degree.items(), key=lambda x: x[1], reverse=True)[:5]:
        node = by_id.get(nid, {})
        top_hubs.append({
            "id": nid,
            "label": _node_name(node),
            "degree": deg,
            "source_file": node.get("source_file"),
        })

    communities: dict[Any, list[dict]] = defaultdict(list)
    for n in nodes:
        c = n.get("community")
        if c is not None:
            communities[c].append(n)

    top_communities = []
    for cid, members in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        top_communities.append({
            "id": cid,
            "size": len(members),
            "sample": [_node_name(m) for m in members[:5]],
        })

    file_types = Counter(n.get("file_type", "unknown") for n in nodes)

    # search_index: lista completa e leve dos símbolos, usada pelo mcp_server
    # para search_symbol cross-repo SEM baixar graph.json.gz. Só id + label +
    # source_file (o mínimo pra o LLM identificar o hit).
    search_index = [
        {
            "id": n.get("id"),
            "label": _node_name(n),
            "source_file": n.get("source_file"),
        }
        for n in nodes
        if n.get("id")
    ]

    summary: dict[str, Any] = {
        "slug": slug,
        "num_nodes": len(nodes),
        "num_links": len(links),
        "num_communities": len(communities),
        "file_types": dict(file_types),
        "top_hubs": top_hubs,
        "top_communities": top_communities,
        "size_bytes_gz": graph_gz.stat().st_size,
        "search_index": search_index,
    }

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            summary["url"] = meta.get("url")
            summary["commit_sha"] = meta.get("commit_sha")
            summary["extracted_at_utc"] = meta.get("extracted_at_utc")
        except json.JSONDecodeError:
            pass

    return summary


def build_index(store_dir: Path, *, write: bool = True) -> dict[str, Any]:
    """Varre store_dir/repos/*/graphify-out/graph.json.gz e gera index.json.

    Se write=True (default), grava store_dir/index.json.
    Retorna o dict do index.
    """
    store_dir = Path(store_dir)
    repos_root = store_dir / "repos"

    index: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repos": {},
    }

    if repos_root.exists():
        for repo_dir in sorted(repos_root.iterdir()):
            if not repo_dir.is_dir():
                continue
            summary = _repo_summary(repo_dir.name, repo_dir)
            if summary is not None:
                index["repos"][repo_dir.name] = summary

    if write:
        index_path = store_dir / "index.json"
        index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        log.info("index.json escrito em %s (%d repos)", index_path, len(index["repos"]))

    return index


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 1:
        store_dir = Path(sys.argv[1])
    else:
        # default: pasta repos/ do teste_graphfy (dev local)
        store_dir = Path(__file__).resolve().parent
    build_index(store_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
