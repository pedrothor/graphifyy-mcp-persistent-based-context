"""Utilitários de extração de grafos (helpers reusados por mongo_store).

Contém: parse_github_url, repo_slug, remote_head_sha, graphify_version,
GRAPHIFY_BIN, e extract_to() (útil para gerar graph.json.gz em disco pra
debug local, se você quiser inspecionar sem passar pelo Mongo).

Sem CLI. Publicação para o Mongo é via MongoStore.publish_repo() ou
tool index_repo do MCP (com MCP_ALLOW_WRITES=true).
"""

from __future__ import annotations

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

log = logging.getLogger("ingest")


def git_env() -> dict[str, str]:
    """Env para chamadas git que impede prompt interativo de credencial.

    Sem isso, no Windows o credential manager pode abrir um prompt silencioso
    (sem TTY = hang eterno) mesmo para repos públicos, se o helper tentar
    autenticar antes de detectar que o repo é aberto.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["GCM_INTERACTIVE"] = "never"
    return env


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


def remote_head_sha(url: str, *, timeout: float = 20.0) -> str | None:
    """Retorna SHA do HEAD remoto sem clonar; None se ls-remote falhar ou expirar."""
    try:
        result = _run(["git", "ls-remote", url, "HEAD"], timeout=timeout, env=git_env())
        first_line = result.stdout.strip().splitlines()[0]
        return first_line.split()[0]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError):
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
    """Extrai o grafo de `url` e escreve em `out_dir` (para debug local).

    Layout final em out_dir:
        graphify-out/graph.json.gz
        meta.json

    Retorna dict com status da operação.
    Publicação para o Mongo é feita separadamente por MongoStore.publish_repo().
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
