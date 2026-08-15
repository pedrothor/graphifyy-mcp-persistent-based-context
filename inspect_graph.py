"""Inspeciona um repo indexado no MongoDB. UX de terminal, sem MCP.

Uso:
    python inspect_graph.py                       # lista repos
    python inspect_graph.py owner__repo           # resumo do repo

Env vars: MONGODB_URI, MONGODB_DB, MONGODB_COLLECTION (mesmas do MCP).
"""

from __future__ import annotations

import sys

from mongo_store import MongoStore


def print_header(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def inspect(store: MongoStore, slug: str) -> None:
    summary = store.get_repo_summary(slug)
    if summary is None:
        print(f"repo '{slug}' não encontrado no store")
        sys.exit(1)

    print(f"# Grafo: {slug}")
    print(f"  nós:      {summary.get('num_nodes', 0):>6}")
    print(f"  arestas:  {summary.get('num_links', 0):>6}")
    print(f"  comm:     {summary.get('num_communities', 0):>6}")
    print(f"  commit:   {summary.get('commit_sha', '?')[:12]}")
    print(f"  url:      {summary.get('url', '?')}")

    print_header("Tipos de nó (file_type)")
    for ft, count in summary.get("file_types", {}).items():
        print(f"  {count:>5}  {ft}")

    print_header("Top 5 hubs (nós mais conectados)")
    for hub in summary.get("top_hubs", []):
        print(f"  {hub.get('degree', 0):>4}  {hub.get('label', ''):<45} {hub.get('source_file', '')}")

    print_header("Top 5 comunidades")
    for c in summary.get("top_communities", []):
        print(f"  community #{c.get('id')} ({c.get('size')} nós)")
        for lab in (c.get("sample") or [])[:5]:
            print(f"      · {lab}")

    print_header("Exemplo de search_symbol('main', limit=3)")
    for hit in store.search_symbol("main", slug=slug, limit=3):
        print(f"  {hit.get('id')} | {hit.get('label')} | {hit.get('source_file', '')}")


def main() -> None:
    store = MongoStore()
    if len(sys.argv) < 2:
        info = store.describe()
        print(f"Store: {info['kind']} @ {info['uri']}")
        print(f"  DB.collection: {info['database']}.{info['collection']}")
        print(f"  Total docs: {info['total_documents']} ({info['repos']} repos)")
        print()
        repos = store.list_repo_slugs()
        if not repos:
            print("Nenhum repo indexado. Rode: python publish_to_mongo.py <url>")
            return
        print("Repos indexados (passe um como argumento):")
        for r in repos:
            print(f"  · {r}")
        return
    inspect(store, sys.argv[1])


if __name__ == "__main__":
    main()
