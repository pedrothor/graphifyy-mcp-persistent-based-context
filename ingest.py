"""Ingestor multi-repo: clona repos GitHub efemeramente e gera graph.json via graphifyy.

Uso:
    python ingest.py https://github.com/tiangolo/typer
    python ingest.py --from-file repos.txt
    python ingest.py --from-file repos.txt --force
"""

from __future__ import annotations

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


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def remote_head_sha(url: str) -> str | None:
    """Retorna SHA do HEAD remoto sem clonar; None se ls-remote falhar."""
    try:
        result = run(["git", "ls-remote", url, "HEAD"])
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


def ingest_one(url: str, force: bool = False) -> bool:
    """Processa um repo. Retorna True se gerou/atualizou o grafo, False se pulou."""
    owner, repo = parse_github_url(url)
    slug = repo_slug(owner, repo)
    out_dir = REPOS_DIR / slug
    meta_path = out_dir / "meta.json"
    graph_path = out_dir / "graphify-out" / "graph.json"

    remote_sha = remote_head_sha(url)
    existing = load_meta(meta_path)

    if (
        not force
        and existing
        and remote_sha
        and existing.get("commit_sha") == remote_sha
        and graph_path.exists()
    ):
        log.info("[%s] SHA remoto igual ao processado (%s), pulando", slug, remote_sha[:8])
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="graphify_ingest_"))
    log.info("[%s] clonando em %s (efêmero)", slug, tmp_dir)

    try:
        run(["git", "clone", "--depth", "1", url, str(tmp_dir)])

        head = run(["git", "-C", str(tmp_dir), "rev-parse", "HEAD"])
        clone_sha = head.stdout.strip()

        out_dir.mkdir(parents=True, exist_ok=True)
        graphify_out = out_dir / "graphify-out"
        if graphify_out.exists():
            shutil.rmtree(graphify_out)

        log.info("[%s] rodando graphify extract --code-only", slug)
        result = subprocess.run(
            [
                GRAPHIFY_BIN,
                "extract",
                str(tmp_dir),
                "--code-only",
                "--out",
                str(out_dir),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error("[%s] graphify extract falhou (rc=%s)", slug, result.returncode)
            if result.stdout:
                log.error("stdout:\n%s", result.stdout)
            if result.stderr:
                log.error("stderr:\n%s", result.stderr)
            return False

        if not graph_path.exists():
            log.error("[%s] extract terminou mas graph.json não foi criado", slug)
            return False

        meta = {
            "url": url,
            "owner": owner,
            "repo": repo,
            "commit_sha": clone_sha,
            "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
            "graphifyy_version": graphify_version(),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log.info("[%s] OK — graph.json em %s", slug, graph_path.relative_to(ROOT))
        return True

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


def main(urls: list[str], force: bool = False) -> int:

    REPOS_DIR.mkdir(exist_ok=True)

    ok = failed = skipped = 0
    for url in urls:
        try:
            changed = ingest_one(url, force=force)
            if changed:
                ok += 1
            else:
                skipped += 1
        except Exception as exc:
            log.exception("falha processando %s: %s", url, exc)
            failed += 1

    log.info("--- resumo: %d gerados, %d pulados, %d falharam ---", ok, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    main(urls=["https://github.com/supabase/supabase"])
