"""Inspeciona um grafo indexado e imprime resumo legível.

Uso:
    python inspect_graph.py                       # lista repos disponíveis
    python inspect_graph.py tiangolo__typer       # resumo do repo
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPOS_DIR = ROOT / "repos"


def list_repos() -> list[str]:
    if not REPOS_DIR.exists():
        return []
    return sorted(
        d.name for d in REPOS_DIR.iterdir()
        if d.is_dir() and (d / "graphify-out" / "graph.json").exists()
    )


def print_header(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def inspect(repo: str) -> None:
    graph_path = REPOS_DIR / repo / "graphify-out" / "graph.json"
    if not graph_path.exists():
        print(f"repo '{repo}' não encontrado ou sem graph.json")
        sys.exit(1)

    with graph_path.open(encoding="utf-8") as f:
        g = json.load(f)

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
    print(f"Dica: abra a visualização HTML em")
    print(f"  {REPOS_DIR / repo / 'graphify-out' / 'GRAPH_TREE.html'}")
    print(f"(gere com: graphify tree --graph {graph_path})")


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
