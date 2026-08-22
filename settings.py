"""Configuração central via pydantic-settings.

Ordem de precedência (do maior pro menor):
    1. Env vars do processo
    2. Arquivo .env no cwd
    3. Defaults abaixo

STORAGE_MODE:
    "local"   -> usa montydb (SQLite em LOCAL_DATA_DIR) — não precisa de MongoDB
    "remote"  -> usa MongoDB via MONGODB_URI
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    storage_mode: Literal["local", "remote"] = Field(
        "local", alias="STORAGE_MODE"
    )
    local_data_dir: Path = Field(
        Path("./data"), alias="LOCAL_DATA_DIR"
    )

    mongodb_uri: str = Field(
        "mongodb://localhost:27017/", alias="MONGODB_URI"
    )
    mongodb_db: str = Field("elos_agent", alias="MONGODB_DB")
    mongodb_collection: str = Field("mcp", alias="MONGODB_COLLECTION")

    mcp_allow_writes: bool = Field(False, alias="MCP_ALLOW_WRITES")

    # Projeto default para writes e filtros de leitura. Se definido, sobrepõe
    # qualquer valor que o agente/cliente passar (trava o server em 1 projeto).
    # Se vazio, o agente controla per-call e reads sem project retornam tudo.
    default_project: str | None = Field(None, alias="DEFAULT_PROJECT")

    # Transport do servidor MCP.
    #   "stdio" -> spawn local pelo cliente (VS Code, Claude Desktop). Default.
    #   "http"  -> serve streamable HTTP via uvicorn em MCP_HOST:MCP_PORT.
    mcp_transport: Literal["stdio", "http"] = Field(
        "stdio", alias="MCP_TRANSPORT"
    )
    mcp_host: str = Field("127.0.0.1", alias="MCP_HOST")
    mcp_port: int = Field(8000, alias="MCP_PORT")


settings = Settings()
