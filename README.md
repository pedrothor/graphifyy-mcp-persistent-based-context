# graphifyy-mcp-persistent-based-context

Pipeline em Python que transforma repositórios GitHub em **grafos de conhecimento** (via [graphifyy](https://pypi.org/project/graphifyy/), tree-sitter, 100% local) e serve tudo para AI coding assistants via **MCP** — de um único endpoint, com consultas cross-repo.

**Novidade da v0.2**: o próprio MCP é o orquestrador. Você fala no chat *"indexa https://github.com/foo/bar"*, o MCP clona efemeramente, extrai o grafo, comprime, e faz push num repo GitHub dedicado (o "store"). As consultas seguintes leem desse store local — nada de rodar CLI manualmente.

## Arquitetura

```
Você (no chat): "indexa https://github.com/foo/bar"
   │
   ▼
Claude chama tool: index_repo(url)
   │
mcp_server.py:
   1. clone efêmero em %TEMP%
   2. graphify extract --code-only
   3. comprime graph.json → graph.json.gz  (~6-25× menor)
   4. git pull no clone local do STORE
   5. copia .gz + meta.json pra dentro
   6. atualiza index.json (stats + hubs + comunidades)
   7. git add + commit + push  (autenticação: gh CLI local)
   8. apaga tempdir
   │
   ▼
Você: "quais são os PaymentStatus disponíveis?"
   │
Claude chama: search_symbol("PaymentStatus")
mcp_server.py: lê graph.json.gz do STORE local (cache LRU por bytes)
   │
   ▼
Resposta cross-repo em ~5ms
```

## Por que grafo em vez de `.md` gerado por LLM

| | `.md` de arquitetura via LLM | Grafo AST (esta abordagem) |
|---|---|---|
| Custo de geração | Tokens × arquivos × cada re-run | Zero API, segundos por repo (só CPU) |
| Determinismo | Regeneração vira texto diferente | Mesmo commit → mesmo grafo, byte-a-byte |
| Consumo no chat | LLM relê o `.md` inteiro (~4k tokens) | Consulta pontual (~500–2k tokens) |
| Erros | LLM pode alucinar relações | Arestas tagueadas `EXTRACTED` vs `INFERRED` |
| Escala | Desatualiza a cada commit | `index_repo` refaz só quando muda |

## Privacidade

- Extração 100% local via tree-sitter. Sem chamadas a APIs externas, sem chave, sem LLM na hora do extract.
- Clone é **efêmero**: `git clone --depth 1` em `%TEMP%`, extrai, `shutil.rmtree` em `finally`. Só sobra o `graph.json.gz` derivado.
- Repos privados usam suas credenciais git locais (gh CLI, SSH, Windows Credential Manager). Nenhum token embutido no código.

## Setup

Requer Python ≥3.10, `git`, e `gh` autenticado (para push no store).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
graphify --version   # confirma
gh auth status       # confirma
```

## Storage dos grafos: `mcp-graph-store`

Grafos ficam num repo GitHub separado (default: [pedrothor/mcp-graph-store](https://github.com/pedrothor/mcp-graph-store)). O MCP clona esse repo no startup, faz `git pull` periódico e faz `git push` a cada nova indexação.

Estrutura do store:

```
mcp-graph-store/
├── index.json              # metadados leves de todos os repos (~KB por repo)
├── .gitignore              # ignora arquivos internos do graphify
└── repos/
    └── {owner}__{repo}/
        ├── graphify-out/
        │   └── graph.json.gz    # grafo comprimido
        └── meta.json            # url, commit_sha, extracted_at_utc
```

## Registrar no Claude Code

Crie `.mcp.json` na raiz do projeto (fica gitignored — cada dev tem o seu):

```json
{
  "mcpServers": {
    "repos-graph": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "env": {
        "MCP_GRAPH_STORE_DIR": "C:\\Users\\SEU_USER\\.mcp-graph-store\\repo",
        "MCP_GRAPH_STORE_URL": "https://github.com/pedrothor/mcp-graph-store.git",
        "MCP_GRAPH_CACHE_MB": "200"
      }
    }
  }
}
```

No chat: `/mcp` deve mostrar `repos-graph` conectado.

### Env vars

| Variável | Default | Descrição |
|---|---|---|
| `MCP_GRAPH_STORE_DIR` | `~/.mcp-graph-store/repo` | Clone local do store |
| `MCP_GRAPH_STORE_URL` | `https://github.com/pedrothor/mcp-graph-store.git` | Repo git que hospeda os grafos |
| `MCP_GRAPH_CACHE_MB` | `200` | Limite do LRU em MB (grafos descompactados) |

## Tools expostas

### Leitura (baratas, sem I/O de rede)

| Tool | O que faz |
|---|---|
| `list_repos()` | Slugs dos repos no store |
| `describe_repos()` | Retorna o `index.json` inteiro (stats + hubs + comunidades) |
| `get_repo_summary(repo)` | Summary de um repo específico |
| `get_node(repo, node_id)` | Detalhes de um símbolo |
| `search_symbol(pattern, repo?)` | Busca por substring; se `repo=None`, cross-repo |
| `get_neighbors(repo, node_id, depth)` | Vizinhança em profundidade N |
| `shortest_path(repo, source, target)` | Menor caminho entre dois símbolos |
| `list_communities(repo, top_n)` | Comunidades Leiden (subsistemas) |

### Escrita (rede + git)

| Tool | O que faz |
|---|---|
| `index_repo(url, force=False)` | Extrai + comprime + push no store |
| `remove_repo(slug)` | Remove um repo do store + push |
| `refresh_store()` | `git pull` (útil se outra máquina publicou) |

## Uso legado: CLI local (sem MCP)

O `ingest.py` ainda funciona pra dev/debug local, populando `./repos/`:

```powershell
python ingest.py https://github.com/tiangolo/typer
python ingest.py --from-file repos.txt
```

E `inspect_graph.py` dá visão textual amigável de qualquer grafo:

```powershell
python inspect_graph.py                      # lista repos
python inspect_graph.py tiangolo__typer      # resumo
```

Ele detecta automaticamente se deve olhar `./repos/` (local) ou `$MCP_GRAPH_STORE_DIR/repos/` (store).

## Estrutura de arquivos

```
.
├── ingest.py            # extract_to() + CLI
├── build_index.py       # build_index() + CLI (gera index.json)
├── mcp_server.py        # servidor MCP (FastMCP stdio)
├── inspect_graph.py     # resumo textual de um grafo
├── requirements.txt
├── repos.txt            # (opcional) lote para CLI ingest
└── repos/               # (gitignored) grafos locais quando usa ingest.py
```

## Ordem de grandeza de custo por consulta

Uma pergunta típica ao MCP consome entre 500 e 5.000 tokens — as tools retornam só o slice relevante do grafo, não o dump. O `graph.json` do `supabase/supabase` inteiro tem ~22M tokens (inviável); uma consulta específica sobre ele retorna ~1.3k tokens.

## Limitações conhecidas

- Modo `--code-only` (usado aqui) pula extração semântica de docs/PDFs/imagens. `deep` requer chave LLM (Anthropic/OpenAI/Gemini/Ollama).
- Grafo captura **estrutura**, não regra de negócio ou decisões arquiteturais. Mantenha ADRs em `.md` no repo original.
- Autenticação git do MCP hoje usa credencial local (via `gh auth`). Para deploy em servidor, migrar para PAT em env var.
- Grafos > 100 MB comprimidos exigiriam `git-lfs` — não é o caso na prática.

## Licença

MIT.
