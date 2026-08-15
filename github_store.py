"""Store provider: lê grafos direto de um repo GitHub via raw.githubusercontent.com.

Adequado para deploy stateless (pod K8s, Cloud Run). Sem git, sem disco persistente,
sem credenciais para push — apenas HTTP GET.

Env vars:
    MCP_GITHUB_REPO      "owner/repo" do store (ex: pedrothor/mcp-graph-store)
    MCP_GITHUB_BRANCH    branch (default: main)
    MCP_GITHUB_TOKEN     PAT opcional (5000 req/h autenticado vs. rate limit
                          implícito anônimo)
    GITHUB_TOKEN         fallback padrão (compat com GitHub Actions)
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("github_store")


class GitHubStoreError(RuntimeError):
    pass


class GitHubStore:
    def __init__(
        self,
        repo: str | None = None,
        branch: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.repo = repo or os.environ.get("MCP_GITHUB_REPO")
        if not self.repo or "/" not in self.repo:
            raise GitHubStoreError(
                "MCP_GITHUB_REPO deve estar setada no formato 'owner/repo'"
            )
        self.branch = branch or os.environ.get("MCP_GITHUB_BRANCH", "main")
        self.token = token or os.environ.get("MCP_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self._base = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}"
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"User-Agent": "graphifyy-mcp/0.4"}
        if self.token:
            # GitHub aceita "token X" (classic PATs) e "Bearer X" (fine-grained);
            # "Bearer" funciona para ambos e é o formato preferido atual.
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _fetch_bytes(self, path: str) -> bytes:
        url = f"{self._base}/{path.lstrip('/')}"
        log.debug("GET %s", url)
        r = self._client.get(url, headers=self._headers())
        if r.status_code == 404:
            raise GitHubStoreError(f"Not found: {path} (branch={self.branch})")
        if r.status_code == 403 and "rate limit" in (r.text or "").lower():
            raise GitHubStoreError(
                "Rate limit atingido. Configure MCP_GITHUB_TOKEN para 5000 req/h."
            )
        r.raise_for_status()
        return r.content

    def fetch_index(self) -> dict[str, Any]:
        """Baixa index.json (metadados leves de todos os repos)."""
        raw = self._fetch_bytes("index.json")
        return json.loads(raw)

    def fetch_graph_gz(self, slug: str) -> bytes:
        """Baixa graph.json.gz bruto (para armazenar no cache LRU sem descompactar)."""
        return self._fetch_bytes(f"repos/{slug}/graphify-out/graph.json.gz")

    def fetch_graph(self, slug: str) -> dict[str, Any]:
        """Baixa e descompacta o grafo em memória."""
        gz = self.fetch_graph_gz(slug)
        with gzip.open(io.BytesIO(gz), "rt", encoding="utf-8") as f:
            return json.load(f)

    def describe(self) -> dict[str, str]:
        return {
            "kind": "github",
            "repo": self.repo,
            "branch": self.branch,
            "authenticated": bool(self.token),
            "base_url": self._base,
        }

    def close(self) -> None:
        self._client.close()
