"""Inspeciona um grafo indexado e imprime resumo legível.

Uso:
    python inspect_graph.py                       # lista repos disponíveis
    python inspect_graph.py tiangolo__typer       # resumo do repo
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPOS_DIR = ROOT / "repos"


def _repos_dir() -> Path:
    """Store dir: prioriza MCP_GRAPH_STORE_DIR (V3), senão usa ./repos (V1 local)."""
    env = os.environ.get("MCP_GRAPH_STORE_DIR")
    if env:
        p = Path(env) / "repos"
        if p.exists():
            return p
    return DEFAULT_REPOS_DIR


def _graph_file(repo_dir: Path) -> Path | None:
    """Aceita graph.json.gz (novo) ou graph.json (legado)."""
    gz = repo_dir / "graphify-out" / "graph.json.gz"
    if gz.exists():
        return gz
    js = repo_dir / "graphify-out" / "graph.json"
    if js.exists():
        return js
    return None


def _load_graph(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def list_repos() -> list[str]:
    repos_dir = _repos_dir()
    if not repos_dir.exists():
        return []
    return sorted(
        d.name for d in repos_dir.iterdir()
        if d.is_dir() and _graph_file(d) is not None
    )


def print_header(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def inspect(repo: str) -> None:
    repos_dir = _repos_dir()
    repo_dir = repos_dir / repo
    graph_path = _graph_file(repo_dir)
    if graph_path is None:
        print(f"repo '{repo}' não encontrado em {repos_dir}")
        sys.exit(1)

    g = _load_graph(graph_path)

    nodes = g.get("nodes", [])
    links = g.get("links", [])

    print(f"# Grafo: {repo}")
    print(f"  nós:     {len(nodes):>6}")
    print(f"  arestas: {len(links):>6}")

    # tipos de arquivo
    file_types = Counter(n.get("file_type", "unknown") for n in nodes)
    print_header("Tipos de nó (file_type)")
    for ft, count in file_types.most_common():
        print(f"  {count:>5}  {ft}")

    # tipos de relação nas arestas
    relations = Counter(l.get("relation", "unknown") for l in links)
    print_header("Tipos de aresta (relation)")
    for rel, count in relations.most_common():
        print(f"  {count:>5}  {rel}")

    # confidence
    confidences = Counter(l.get("confidence", "unknown") for l in links)
    print_header("Confiança das arestas (confidence)")
    for conf, count in confidences.most_common():
        print(f"  {count:>5}  {conf}")

    # graus (hubs) — nós mais conectados
    degree: dict[str, int] = defaultdict(int)
    for l in links:
        src = l.get("source"); tgt = l.get("target")
        if src: degree[src] += 1
        if tgt: degree[tgt] += 1

    by_id = {n.get("id"): n for n in nodes}
    top = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:10]
    print_header("Top 10 hubs (nós com mais conexões)")
    for nid, deg in top:
        node = by_id.get(nid, {})
        label = node.get("label") or node.get("norm_label") or nid
        src = node.get("source_file", "")
        loc = node.get("source_location", "")
        print(f"  {deg:>4} conexões  {label:<45} {src}{':' + loc if loc else ''}")

    # comunidades
    communities: dict = defaultdict(list)
    for n in nodes:
        c = n.get("community")
        if c is not None:
            communities[c].append(n)
    top_c = sorted(communities.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    print_header("Top 5 comunidades (subsistemas)")
    for cid, members in top_c:
        labels = [m.get("label") or m.get("norm_label") or m.get("id", "?") for m in members[:6]]
        print(f"  community #{cid}  ({len(members)} nós)")
        for lab in labels:
            print(f"      · {lab}")

    # exemplo de aresta (primeira)
    if links:
        print_header("Exemplo de aresta (primeira do arquivo)")
        first = links[0]
        src = by_id.get(first.get("source"), {})
        tgt = by_id.get(first.get("target"), {})
        print(f"  {src.get('label', first.get('source'))}")
        print(f"    --[{first.get('relation')}, {first.get('confidence')}]-->")
        print(f"  {tgt.get('label', first.get('target'))}")

    print()
    print(f"Dica: gere visualização HTML com")
    print(f"  graphify tree --graph {graph_path}")


def main() -> None:
    if len(sys.argv) < 2:
        repos = list_repos()
        if not repos:
            print("Nenhum repo indexado. Rode: python ingest.py <github-url>")
            return
        print("Repos indexados (passe um como argumento):")
        for r in repos:
            print(f"  · {r}")
        return
    inspect(sys.argv[1])


if __name__ == "__main__":
    main()
