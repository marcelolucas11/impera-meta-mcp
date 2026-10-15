# Impera Meta MCP: instruções para o Claude

Repositório do app Impera Meta MCP (Mac) e do servidor MCP `meta-ads`. O dono é o Marcelo (Impera Services). Ele pede mudanças na conversa e o Claude aplica, testa e publica aqui.

## Fluxo de cada atualização

1. Aplicar a mudança.
2. Rodar `bash testes/rodar.sh` (com `CHROMIUM_PATH` se o Playwright não tiver Chromium próprio). Só publicar com tudo passando; teste novo para comportamento novo.
3. Subir a versão em `VERSAO` (X.Y.Z) e escrever a seção `## X.Y.Z` no topo de `NOVIDADES.md`, em português e do ponto de vista de quem usa o app. Essa seção vira a nota da release e aparece no app em "O que mudou".
4. Commit em português, explicando o porquê. Push para `main`.
5. O push na `main` basta: o GitHub Actions (`.github/workflows/publicar.yml`) roda os testes e, se a `VERSAO` ainda não tiver release, cria a tag `vX.Y.Z`, monta `impera-meta-mcp.zip` + `.sha256` com `empacotar.sh` e publica a release como "latest". Não criar tag à mão (o proxy da sessão não permite enviar tags).
6. Conferir que a release saiu com os dois arquivos. A partir daí o botão "Buscar atualização" do app de todo mundo oferece a versão nova.
7. Dizer ao Marcelo o que mudou e lembrar: atualizar pelo app e reiniciar o Claude (Cmd+Q).

## Atualização sem perda de dados

- `atualizar.py` e `instalar.sh` só escrevem arquivos do programa. Nunca tocam em `clientes/`, `.env`, `relatorios/`, `.venv/`, `.painel.json`, `painel.log`, `.versoes-anteriores/`.
- Qualquer arquivo novo de dados do usuário precisa entrar nessas duas listas de preservação (e no `.gitignore`) antes de existir.
- O nome do pacote (`impera-meta-mcp.zip`) e a pasta raiz dele (`impera-meta-mcp/`) não mudam: o app instalado e o botão de download do README dependem deles.

## Regras que não mudam

- Somente leitura: o servidor só faz GET na Graph API. Nada que crie, edite, pause ou apague.
- Token só no cabeçalho `Authorization`, nunca em URL, log, resposta de ferramenta ou na janela do app. Nunca pedir token no chat.
- Um token por cliente (`clientes/<id>.env`, permissão 600). Com mais de um cliente, `cliente` é obrigatório: nunca escolher por omissão.
- Nenhum token, `.env`, `clientes/` ou `relatorios/` no repositório.
- O nome do servidor no Claude Desktop é `meta-ads` e a pasta instalada é `~/meta-ads-mcp`. Mudar qualquer um quebra instalações e instruções de projeto já existentes.
- Alcance e frequência são deduplicados: nunca somar entre linhas.
