"""Inspeciona um repo indexado no MongoDB. Funções puras, sem CLI.

Uso programático:
    from inspect_graph import inspect_repo, list_indexed_repos
    from mongo_store import MongoStore

    store = MongoStore()
    print(list_indexed_repos(store))
    inspect_repo(store, "owner__repo")   # imprime resumo no stdout
"""

from __future__ import annotations

import sys

from mongo_store import MongoStore


def _print_header(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def list_indexed_repos(store: MongoStore) -> list[str]:
    """Retorna slugs de todos os repos no store."""
    return store.list_repo_slugs()


def inspect_repo(store: MongoStore, slug: str, *, out=sys.stdout) -> None:
    """Imprime resumo textual do repo em `out`."""
    def w(s: str = "") -> None:
        print(s, file=out)

    summary = store.get_repo_summary(slug)
    if summary is None:
        w(f"repo '{slug}' não encontrado no store")
        return

    w(f"# Grafo: {slug}")
    w(f"  nós:      {summary.get('num_nodes', 0):>6}")
    w(f"  arestas:  {summary.get('num_links', 0):>6}")
    w(f"  comm:     {summary.get('num_communities', 0):>6}")
    w(f"  commit:   {summary.get('commit_sha', '?')[:12]}")
    w(f"  url:      {summary.get('url', '?')}")

    _print_header("Tipos de nó (file_type)")
    for ft, count in summary.get("file_types", {}).items():
        w(f"  {count:>5}  {ft}")

    _print_header("Top 5 hubs (nós mais conectados)")
    for hub in summary.get("top_hubs", []):
        w(f"  {hub.get('degree', 0):>4}  {hub.get('label', ''):<45} {hub.get('source_file', '')}")

    _print_header("Top 5 comunidades")
    for c in summary.get("top_communities", []):
        w(f"  community #{c.get('id')} ({c.get('size')} nós)")
        for lab in (c.get("sample") or [])[:5]:
            w(f"      · {lab}")


def store_overview(store: MongoStore, *, out=sys.stdout) -> None:
    """Imprime info geral do store + lista de repos."""
    def w(s: str = "") -> None:
        print(s, file=out)

    info = store.describe()
    w(f"Store: {info['kind']} @ {info['uri']}")
    w(f"  DB.collection: {info['database']}.{info['collection']}")
    if info.get("connected"):
        w(f"  Total docs: {info['total_documents']} ({info['repos']} repos)")
    else:
        w(f"  offline: {info.get('error', 'unknown')}")
    w()

    repos = list_indexed_repos(store)
    if not repos:
        w("Nenhum repo indexado. Use MongoStore.publish_repo(url) para adicionar.")
        return
    w("Repos indexados:")
    for r in repos:
        w(f"  · {r}")
