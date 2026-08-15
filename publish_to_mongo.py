"""Publica grafos de repos GitHub direto no MongoDB.

Uso:
    python publish_to_mongo.py https://github.com/foo/bar
    python publish_to_mongo.py --from-file repos.txt
    python publish_to_mongo.py --from-file repos.txt --force
    python publish_to_mongo.py --remove owner__repo

Env vars (mesmas do MCP):
    MONGODB_URI, MONGODB_DB, MONGODB_COLLECTION
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from mongo_store import MongoStore


def read_urls_file(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    log = logging.getLogger("publish")

    parser = argparse.ArgumentParser(description="Publica grafos no MongoDB")
    parser.add_argument("urls", nargs="*", help="URLs de repos GitHub")
    parser.add_argument("--from-file", type=Path, help="Arquivo com uma URL por linha")
    parser.add_argument("--force", action="store_true", help="Re-extrai mesmo se SHA igual")
    parser.add_argument("--remove", metavar="SLUG", help="Remove um repo do store")
    args = parser.parse_args()

    store = MongoStore()
    log.info("connected: %s", store.describe())

    if args.remove:
        r = store.remove_repo(args.remove)
        log.info("remove: %s", r)
        return 0

    urls: list[str] = list(args.urls)
    if args.from_file:
        urls.extend(read_urls_file(args.from_file))
    if not urls:
        parser.error("informe ao menos uma URL, --from-file, ou --remove")

    ok = skipped = failed = 0
    for url in urls:
        try:
            r = store.publish_repo(url, force=args.force)
            status = r.get("status")
            if status == "extracted":
                ok += 1
                log.info(
                    "OK [%s] %d nós, %d arestas @ %s",
                    r["slug"], r["num_nodes"], r["num_links"], r["commit_sha"][:8],
                )
            elif status == "skipped":
                skipped += 1
                log.info("SKIP [%s] %s", r["slug"], r.get("reason", ""))
            else:
                failed += 1
                log.error("FAIL [%s] %s", r.get("slug", "?"), r.get("reason", "?"))
        except Exception as exc:
            log.exception("erro processando %s: %s", url, exc)
            failed += 1

    log.info("--- resumo: %d publicados, %d pulados, %d falharam ---", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
