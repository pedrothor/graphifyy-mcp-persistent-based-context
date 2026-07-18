# graphifyy-mcp-persistent-based-context

Pipeline para transformar repositórios GitHub em **grafos de conhecimento locais** e servi-los para AI coding assistants via **MCP** (Model Context Protocol).

Combina a lib [graphifyy](https://pypi.org/project/graphifyy/) (extração de código via tree-sitter, 100% local, sem LLM) com um servidor MCP customizado que expõe **múltiplos repos em um único endpoint**, permitindo consultas cross-repo pelo Claude Code, Cursor, ou qualquer cliente MCP.

## Por que grafo em vez de `.md` gerado por LLM

| | `.md` de arquitetura via LLM | Grafo AST (esta abordagem) |
|---|---|---|
| Custo de geração | Tokens × arquivos × cada re-run | Zero API, segundos por repo (só CPU) |
| Determinismo | Regeneração vira texto diferente | Mesmo commit → mesmo grafo, byte-a-byte |
| Consumo no chat | LLM relê o `.md` inteiro (~4k tokens) | Consulta pontual (~500–2k tokens) |
| Erros | LLM pode alucinar relações | Arestas tagueadas `EXTRACTED` vs `INFERRED` |
| Escala | Desatualiza a cada commit | `graphify update` refaz só o que mudou |

O grafo é o **banco**. As tools MCP são as **queries**. O LLM só vê os resultados.

## Privacidade

- Extração de código é **100% local** via tree-sitter. Sem chamadas a APIs externas, sem chave, sem LLM.
- Clone é **efêmero**: `git clone --depth 1` em `%TEMP%`, extração, `shutil.rmtree` em `finally`. Só sobra o `graph.json` derivado.
- Repos privados usam suas credenciais git locais (SSH agent, credential helper). Nenhum token embutido no código.

## Componentes

```
GitHub URL ──► ingest.py ──► repos/{owner}__{repo}/
                              ├── graphify-out/graph.json
                              └── meta.json
                                    ▲
                                    │ lê múltiplos graph.json
                             mcp_server.py (FastMCP stdio)
                                    ▲
                                    │ MCP
                              Claude Code / Cursor
```

## Setup

Requer Python ≥3.10 e `git` no PATH.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
graphify --version   # confirma instalação
```

## Ingestão

Um repo por vez:

```bash
python ingest.py https://github.com/tiangolo/typer
```

Em lote via `repos.txt` (uma URL por linha, `#` para comentário):

```bash
python ingest.py --from-file repos.txt
```

Modo idempotente: se o SHA remoto do HEAD não mudou, pula. Force com `--force`.

## Servidor MCP

```bash
python mcp_server.py   # stdio, padrão
```

Registre no Claude Code criando um `.mcp.json` na raiz do projeto (**não commitado**, gitignored):

```json
{
  "mcpServers": {
    "repos-graph": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"]
    }
  }
}
```

No chat: digite `/mcp` para confirmar que `repos-graph` está conectado.

### Tools expostas

| Tool | O que faz |
|---|---|
| `list_repos()` | Lista repos indexados |
| `get_repo_summary(repo)` | Metadados + stats + tipos de arquivo |
| `get_node(repo, node_id)` | Detalhes de um símbolo |
| `search_symbol(pattern, repo?)` | Busca de símbolo global ou por repo |
| `get_neighbors(repo, node_id, depth)` | Vizinhança até profundidade N |
| `shortest_path(repo, source, target)` | Menor caminho entre dois símbolos |
| `list_communities(repo, top_n)` | Comunidades Leiden (subsistemas) |

## Inspeção rápida (sem MCP)

Para olhar um grafo direto no terminal, use `inspect_graph.py`:

```bash
python inspect_graph.py                        # lista repos
python inspect_graph.py tiangolo__typer        # resumo do repo
```

Mostra: hubs (nós mais conectados), tipos de aresta, distribuição de confiança e top comunidades.

Para uma visualização HTML interativa:

```bash
graphify tree --graph repos/tiangolo__typer/graphify-out/graph.json \
              --output repos/tiangolo__typer/graphify-out/GRAPH_TREE.html
```

## Estrutura de arquivos

```
.
├── ingest.py            # ingestor multi-repo (clone efêmero)
├── mcp_server.py        # servidor MCP (FastMCP, stdio)
├── inspect_graph.py     # resumo textual de um grafo
├── requirements.txt
├── repos.txt            # (opcional) lista de URLs para batch
└── repos/               # (gitignored) grafos gerados
    └── {owner}__{repo}/
        ├── graphify-out/
        │   ├── graph.json
        │   └── manifest.json
        └── meta.json    # url, commit_sha, extracted_at_utc
```

## Ordem de grandeza de custo por consulta

Uma pergunta típica ao MCP consome entre 500 e 5.000 tokens, independentemente do tamanho do repo — as tools retornam só o slice relevante do grafo, não o dump completo. O `graph.json` do `supabase/supabase` inteiro tem ~22M tokens (inviável de injetar); uma consulta específica sobre ele retorna ~1.3k tokens.

## Limitações conhecidas

- Modo `--code-only` (usado aqui) pula extração semântica de docs/PDFs/imagens. Trocar para modo `deep` requer chave LLM (Anthropic/OpenAI/Gemini/Ollama).
- Grafo captura **estrutura**, não **regra de negócio** ou **decisões arquiteturais**. Mantenha ADRs em `.md` separados no repo original.
- Cache em memória no MCP server: repos grandes (>50k nós) podem consumir centenas de MB de RAM.

## Licença

MIT.
