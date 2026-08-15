"""Ingestor multi-repo: clona repos GitHub efemeramente e gera graph.json.gz.

Uso CLI:
    python ingest.py https://github.com/tiangolo/typer
    python ingest.py --from-file repos.txt
    python ingest.py --from-file repos.txt --force

Uso programático (reusado pelo mcp_server.py):
    from ingest import extract_to, parse_github_url, repo_slug
    result = extract_to(url, out_dir)
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPOS_DIR = ROOT / "repos"


def _resolve_graphify() -> str:
    """Localiza o executável graphify (mesmo com venv não ativado)."""
    candidates = [
        Path(sys.executable).parent / ("graphify.exe" if os.name == "nt" else "graphify"),
        Path(sys.executable).parent / "Scripts" / "graphify.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "graphify"


GRAPHIFY_BIN = _resolve_graphify()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest")


def parse_github_url(url: str) -> tuple[str, str]:
    """Extrai (owner, repo) de uma URL GitHub. Aceita https e ssh."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", url)
    if not m:
        raise ValueError(f"URL GitHub inválida: {url}")
    return m.group(1), m.group(2)


def repo_slug(owner: str, repo: str) -> str:
    return f"{owner}__{repo}"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def remote_head_sha(url: str) -> str | None:
    """Retorna SHA do HEAD remoto sem clonar; None se ls-remote falhar."""
    try:
        result = _run(["git", "ls-remote", url, "HEAD"])
        first_line = result.stdout.strip().splitlines()[0]
        return first_line.split()[0]
    except (subprocess.CalledProcessError, IndexError):
        return None


def load_meta(meta_path: Path) -> dict | None:
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def graphify_version() -> str:
    try:
        result = subprocess.run(
            [GRAPHIFY_BIN, "--version"], capture_output=True, text=True, timeout=10
        )
        return (result.stdout or result.stderr).strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _compress_graph(graph_json: Path) -> Path:
    """Comprime graph.json em graph.json.gz (nível 6) e deleta o original."""
    gz_path = graph_json.with_suffix(graph_json.suffix + ".gz")
    with graph_json.open("rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    graph_json.unlink()
    return gz_path


def _summarize_graph(graph_gz: Path) -> tuple[int, int]:
    """Lê graph.json.gz e retorna (num_nodes, num_links)."""
    with gzip.open(graph_gz, "rt", encoding="utf-8") as f:
        data = json.load(f)
    return len(data.get("nodes", [])), len(data.get("links", []))


def extract_to(url: str, out_dir: Path, *, force: bool = False) -> dict:
    """Extrai o grafo de `url` e escreve em `out_dir`.

    Layout final em out_dir:
        graphify-out/graph.json.gz
        meta.json

    Retorna dict com status da operação:
        {
          "status": "extracted" | "skipped" | "failed",
          "slug": "owner__repo",
          "url": ...,
          "commit_sha": "...",
          "graph_path": Path,
          "num_nodes": int,
          "num_links": int,
          "size_bytes_gz": int,
          "reason": "..." (só quando skipped ou failed)
        }
    """
    owner, repo = parse_github_url(url)
    slug = repo_slug(owner, repo)
    out_dir = Path(out_dir)
    meta_path = out_dir / "meta.json"
    graph_gz = out_dir / "graphify-out" / "graph.json.gz"

    remote_sha = remote_head_sha(url)
    existing = load_meta(meta_path)

    if (
        not force
        and existing
        and remote_sha
        and existing.get("commit_sha") == remote_sha
        and graph_gz.exists()
    ):
        log.info("[%s] SHA remoto %s já processado, pulando", slug, remote_sha[:8])
        return {
            "status": "skipped",
            "slug": slug,
            "url": url,
            "commit_sha": remote_sha,
            "graph_path": graph_gz,
            "reason": "SHA remoto igual ao já processado",
        }

    tmp_dir = Path(tempfile.mkdtemp(prefix="graphify_ingest_"))
    log.info("[%s] clonando em %s (efêmero)", slug, tmp_dir)

    try:
        _run(["git", "clone", "--depth", "1", url, str(tmp_dir)])

        head = _run(["git", "-C", str(tmp_dir), "rev-parse", "HEAD"])
        clone_sha = head.stdout.strip()

        out_dir.mkdir(parents=True, exist_ok=True)
        graphify_out = out_dir / "graphify-out"
        if graphify_out.exists():
            shutil.rmtree(graphify_out)

        log.info("[%s] rodando graphify extract --code-only", slug)
        result = subprocess.run(
            [GRAPHIFY_BIN, "extract", str(tmp_dir), "--code-only", "--out", str(out_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error("[%s] graphify extract falhou (rc=%s)", slug, result.returncode)
            if result.stdout:
                log.error("stdout:\n%s", result.stdout)
            if result.stderr:
                log.error("stderr:\n%s", result.stderr)
            return {
                "status": "failed",
                "slug": slug,
                "url": url,
                "reason": f"graphify extract rc={result.returncode}",
            }

        graph_json = graphify_out / "graph.json"
        if not graph_json.exists():
            return {
                "status": "failed",
                "slug": slug,
                "url": url,
                "reason": "graph.json não foi gerado pelo extract",
            }

        graph_gz = _compress_graph(graph_json)
        num_nodes, num_links = _summarize_graph(graph_gz)
        size_bytes_gz = graph_gz.stat().st_size

        meta = {
            "url": url,
            "owner": owner,
            "repo": repo,
            "commit_sha": clone_sha,
            "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
            "graphifyy_version": graphify_version(),
            "num_nodes": num_nodes,
            "num_links": num_links,
            "size_bytes_gz": size_bytes_gz,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log.info(
            "[%s] OK — %d nós, %d arestas, %.1f KB comprimido",
            slug, num_nodes, num_links, size_bytes_gz / 1024,
        )
        return {
            "status": "extracted",
            "slug": slug,
            "url": url,
            "commit_sha": clone_sha,
            "graph_path": graph_gz,
            "num_nodes": num_nodes,
            "num_links": num_links,
            "size_bytes_gz": size_bytes_gz,
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("[%s] clone efêmero deletado", slug)


def read_urls_file(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta repos GitHub em grafos graphifyy")
    parser.add_argument("urls", nargs="*", help="URLs de repos GitHub")
    parser.add_argument("--from-file", type=Path, help="Arquivo com uma URL por linha")
    parser.add_argument("--force", action="store_true", help="Re-extrai mesmo se SHA não mudou")
    args = parser.parse_args()

    urls: list[str] = list(args.urls)
    if args.from_file:
        urls.extend(read_urls_file(args.from_file))
    if not urls:
        parser.error("informe ao menos uma URL ou use --from-file")

    REPOS_DIR.mkdir(exist_ok=True)

    ok = skipped = failed = 0
    for url in urls:
        try:
            owner, repo = parse_github_url(url)
            out_dir = REPOS_DIR / repo_slug(owner, repo)
            result = extract_to(url, out_dir, force=args.force)
            if result["status"] == "extracted":
                ok += 1
            elif result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as exc:
            log.exception("falha processando %s: %s", url, exc)
            failed += 1

    log.info("--- resumo: %d gerados, %d pulados, %d falharam ---", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
