"""Store provider: lê grafos de um repo Azure DevOps via REST API.

Adequado quando o store está hospedado em Azure DevOps Repos (não GitHub).

Env vars:
    MCP_AZDO_ORG         organização (ex: contoso — segmento após dev.azure.com/)
    MCP_AZDO_PROJECT     nome do projeto
    MCP_AZDO_REPO        nome do repositório (não o GUID)
    MCP_AZDO_BRANCH      branch (default: main)
    MCP_AZDO_PAT         Personal Access Token com escopo Code (Read)
    AZDO_PAT             fallback padrão

Autenticação: Basic Auth com username vazio (":<PAT>" em base64).
Docs: https://learn.microsoft.com/rest/api/azure/devops/git/items
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("azure_devops_store")

API_VERSION = "7.1"


class AzureDevOpsStoreError(RuntimeError):
    pass


class AzureDevOpsStore:
    def __init__(
        self,
        org: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        pat: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.org = org or os.environ.get("MCP_AZDO_ORG")
        self.project = project or os.environ.get("MCP_AZDO_PROJECT")
        self.repo = repo or os.environ.get("MCP_AZDO_REPO")
        self.branch = branch or os.environ.get("MCP_AZDO_BRANCH", "main")
        self.pat = pat or os.environ.get("MCP_AZDO_PAT") or os.environ.get("AZDO_PAT")

        missing = [
            name for name, value in {
                "MCP_AZDO_ORG": self.org,
                "MCP_AZDO_PROJECT": self.project,
                "MCP_AZDO_REPO": self.repo,
            }.items() if not value
        ]
        if missing:
            raise AzureDevOpsStoreError(f"env vars faltando: {', '.join(missing)}")
        if not self.pat:
            raise AzureDevOpsStoreError("MCP_AZDO_PAT (ou AZDO_PAT) é obrigatório")

        self._base = (
            f"https://dev.azure.com/{quote(self.org)}"
            f"/{quote(self.project)}/_apis/git/repositories/{quote(self.repo)}"
        )
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Accept": accept,
            "User-Agent": "graphifyy-mcp/0.4",
        }

    def _items_url(self, path: str, *, download: bool) -> str:
        # https://learn.microsoft.com/rest/api/azure/devops/git/items/get
        # path deve começar com '/', ex: '/index.json'
        params = [
            f"path={quote(path)}",
            "versionDescriptor.versionType=branch",
            f"versionDescriptor.version={quote(self.branch)}",
            f"api-version={API_VERSION}",
            "includeContent=true",
        ]
        if download:
            params.append("download=true")
            params.append("$format=octetStream")
        return f"{self._base}/items?" + "&".join(params)

    def _get(self, url: str, accept: str) -> httpx.Response:
        log.debug("GET %s", url)
        r = self._client.get(url, headers=self._headers(accept=accept))
        if r.status_code == 404:
            raise AzureDevOpsStoreError(f"Not found: {url}")
        if r.status_code == 401:
            raise AzureDevOpsStoreError("Autenticação falhou (PAT inválido ou sem escopo Code Read).")
        r.raise_for_status()
        return r

    def fetch_index(self) -> dict[str, Any]:
        r = self._get(self._items_url("/index.json", download=False), accept="application/json")
        # se veio como blob octet-stream por alguma configuração de mime, ainda é JSON
        try:
            return r.json()
        except json.JSONDecodeError:
            return json.loads(r.content)

    def fetch_graph_gz(self, slug: str) -> bytes:
        url = self._items_url(
            f"/repos/{slug}/graphify-out/graph.json.gz", download=True
        )
        r = self._get(url, accept="application/octet-stream")
        return r.content

    def fetch_graph(self, slug: str) -> dict[str, Any]:
        gz = self.fetch_graph_gz(slug)
        with gzip.open(io.BytesIO(gz), "rt", encoding="utf-8") as f:
            return json.load(f)

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "azure_devops",
            "org": self.org,
            "project": self.project,
            "repo": self.repo,
            "branch": self.branch,
            "authenticated": bool(self.pat),
            "base_url": self._base,
        }

    def close(self) -> None:
        self._client.close()
