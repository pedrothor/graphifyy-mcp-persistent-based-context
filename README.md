# graphifyy-mcp-persistent-based-context

Pipeline Python que transforma repositórios GitHub em **grafos de conhecimento** (via [graphifyy](https://pypi.org/project/graphifyy/), tree-sitter, 100% local) e serve tudo para AI coding assistants via **MCP** — de um único endpoint, com consultas cross-repo, backed por **MongoDB**.

**v0.5**: storage migrado para MongoDB. O grafo (nodes/links) e metadados de todos os repos ficam numa única collection. Escalável pra 100+ repos, consultas cirúrgicas sem baixar arquivo, RAM constante no pod K8s.

## Arquitetura

```
Cliente MCP (Claude Code / Cursor)
        │
        │ stdio (JSON-RPC)
        ▼
┌──────────────────────────────────────────┐
│ mcp_server.py                             │
│   9 tools de leitura                      │
│   (thin — só delega ao MongoStore)        │
└─────────────────┬────────────────────────┘
                  │  queries indexadas
                  ▼
        ┌──────────────────────┐
        │  MongoDB             │
        │  db: elos_agent      │
        │  collection: mcp     │
        │                      │
        │  _type=repo   → 1 doc por repo indexado
        │  _type=node   → 1 doc por símbolo (função, classe, arquivo…)
        │  _type=link   → 1 doc por relação (calls, imports, references…)
        └──────────────────────┘
                  ▲
                  │  bulk write
                  │
        ┌──────────────────────────┐
        │ MongoStore.publish_repo()│  clona repo efêmero → graphify extract →
        │ (chamada por index_repo  │  converte em docs → bulk insert
        │  do MCP ou código Python)│
        └──────────────────────────┘
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
graphify --version   # confirma
```

Depois configure as env vars (via `.mcp.json` local ou export no shell):

```
MONGODB_URI=mongodb://user:pass@host:27017/       # default: mongodb://localhost:27017/
MONGODB_DB=elos_agent                              # default
MONGODB_COLLECTION=mcp                             # default
MCP_ALLOW_WRITES=true                              # opcional; habilita index_repo tool
```

## Indexar um repo

### Via chat MCP (`index_repo` tool) — dev local

Habilite `MCP_ALLOW_WRITES=true` no `.mcp.json`, reinicie o Claude Code, e no chat:

> "indexa https://github.com/pedrothor/payment-api"

Isso chama `MongoStore.publish_repo(url)`, que faz:
1. `git clone --depth 1` do url em `%TEMP%` (efêmero)
2. `graphify extract --code-only` (tree-sitter, sem LLM, sem chave)
3. Converte `graph.json` em documentos Mongo (`_type: repo/node/link`)
4. `deleteMany({slug})` + `insertMany` (substituição atômica do repo)
5. Deleta o tempdir

### Via código Python (CI/pipeline)

Não há CLI dedicado — importe direto:

```python
from mongo_store import MongoStore

store = MongoStore()  # lê MONGODB_URI/DB/COLLECTION do env
result = store.publish_repo("https://github.com/foo/bar")
print(result)         # {"status": "extracted", "slug": ..., "num_nodes": ..., ...}

# lote
urls = ["https://github.com/a/b", "https://github.com/c/d"]
for url in urls:
    store.publish_repo(url, force=False)

# remover
store.remove_repo("owner__repo")
```

Coloque isso num script do seu CI (GitHub Actions, Azure Pipelines, etc) e agende
como preferir (push, cron, manual).

## Modelagem no MongoDB

**Repo** — metadados + summary:
```javascript
{
  _id: "repo:pedrothor__payment-api",
  _type: "repo",
  slug: "pedrothor__payment-api",
  url: "https://github.com/pedrothor/payment-api",
  commit_sha: "07f6a377...",
  extracted_at_utc: "2026-08-15T...",
  num_nodes: 29, num_links: 67, num_communities: 5,
  file_types: {"code": 25, "rationale": 4},
  top_hubs: [...],
  top_communities: [...],
}
```

**Node** — 1 por símbolo do grafo:
```javascript
{
  _id: "node:pedrothor__payment-api:models_schemas_paymentstatus",
  _type: "node",
  slug: "pedrothor__payment-api",
  node_id: "models_schemas_paymentstatus",
  label: "PaymentStatus",
  source_file: "models/schemas.py",
  source_location: "L7",
  file_type: "code",
  community: 3
}
```

**Link** — 1 por aresta:
```javascript
{
  _id: "link:pedrothor__payment-api:42",
  _type: "link",
  slug: "pedrothor__payment-api",
  source: "api_client",
  target: "schemas_paymentstatus",
  relation: "imports",
  confidence: "EXTRACTED"
}
```

### Índices criados automaticamente (lazy, na primeira operação)

```javascript
(_type, slug)                        // listar tudo do repo
(_type, slug, node_id)               // get_node — chave única
(_type, slug, source)                // BFS de vizinhos (out)
(_type, slug, target)                // BFS de vizinhos (in)
(_type, slug, community)             // list_communities
(_type, label)                       // search_symbol cross-repo (regex + i)
```

## Tools expostas pelo MCP

**Leitura** (todas usam queries indexadas):

| Tool | Query MongoDB |
|---|---|
| `list_repos()` | `find({_type: "repo"}, {slug: 1})` |
| `describe_repos()` | `find({_type: "repo"})` (metadados leves) |
| `get_repo_summary(repo)` | `findOne({_type: "repo", slug})` |
| `search_symbol(pattern, repo?)` | `find({_type: "node", label: /X/i, slug?})` — **cross-repo em 1 query** |
| `get_node(repo, id)` | `findOne({_type: "node", slug, node_id})` |
| `get_neighbors(repo, id, depth)` | BFS: N queries indexadas em `source`/`target` |
| `shortest_path(repo, src, tgt)` | BFS até encontrar (cap 20 níveis) |
| `list_communities(repo, top_n)` | `aggregate: $match + $group` |
| `store_info()` | describe (URI mascarada, contagens) |

**Escrita** (só se `MCP_ALLOW_WRITES=true`):

| Tool | O que faz |
|---|---|
| `index_repo(url, force=False)` | extrai + escreve no Mongo |
| `remove_repo(slug)` | deleta todos os docs do slug |

## Registrar o MCP no Claude Code

`.mcp.json` na raiz do projeto (não versionado):

```json
{
  "mcpServers": {
    "repos-graph": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "env": {
        "MONGODB_URI": "mongodb://user:pass@host:27017/",
        "MONGODB_DB": "elos_agent",
        "MONGODB_COLLECTION": "mcp",
        "MCP_ALLOW_WRITES": "true"
      }
    }
  }
}
```

Reload da janela do VSCode. Digite `/mcp` no chat pra confirmar conexão.

## Deploy stateless em K8s

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: repos-graph-mcp
spec:
  template:
    spec:
      containers:
        - name: mcp
          image: your-registry/repos-graph-mcp:latest
          command: ["python", "mcp_server.py"]
          env:
            - name: MONGODB_URI
              valueFrom: {secretKeyRef: {name: mongo, key: uri}}
            - name: MONGODB_DB
              value: elos_agent
            - name: MONGODB_COLLECTION
              value: mcp
            # MCP_ALLOW_WRITES não setada → read-only em prod
          resources:
            requests: {memory: 128Mi, cpu: 100m}
            limits:   {memory: 256Mi, cpu: 500m}
```

Publicação (indexação de novos repos) roda separadamente num job/pipeline com `MCP_ALLOW_WRITES=true` e credenciais git.

## Ordem de grandeza

- **RAM do pod**: ~50 MB fixos (pymongo client + pool). Escala 0 com número de repos indexados.
- **Query típica**: 5-50 ms (indexada). `search_symbol` cross-repo em ~100 repos: ~50-200 ms.
- **Custo por consulta no LLM**: 500-5000 tokens dependendo da profundidade.
- **Custo de indexar 1 repo**: 5-30s (clone + extract + insert), sem LLM cost.

## Estrutura de arquivos

```
.
├── mcp_server.py         # servidor MCP (thin, delega ao MongoStore)
├── mongo_store.py        # MongoStore: leitura + escrita + índices
├── ingest.py             # utilitários puros (parse_url, extract_to debug local)
├── inspect_graph.py      # funções para resumo textual (importar e chamar)
├── requirements.txt      # graphifyy + mcp + networkx + pymongo
├── repos.txt             # (opcional) lista de URLs para lote
└── repos/                # (gitignored) grafos de debug local
```

## Limitações conhecidas

- Modo `--code-only` pula extração semântica de docs/PDFs/imagens. `deep` requer chave LLM.
- Grafo captura estrutura, não regra de negócio ou decisões arquiteturais.
- `shortest_path` tem cap de 20 níveis (proteção contra grafos com ciclos grandes).
- Escritas do MCP (index_repo) requerem `git` disponível no ambiente do pod.

## Licença

MIT.
