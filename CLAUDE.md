# Impera Meta MCP: instruções para o Claude

Repositório do app Impera Meta MCP (Mac) e do servidor MCP `meta-ads`. O dono é o Marcelo (Impera Services). Ele pede mudanças na conversa e o Claude aplica, testa e publica aqui.

## Fluxo de cada atualização

1. Aplicar a mudança.
2. Rodar `bash testes/rodar.sh` (com `CHROMIUM_PATH` se o Playwright não tiver Chromium próprio). Só publicar com tudo passando; teste novo para comportamento novo.
3. Subir a versão: `CFBundleShortVersionString`/`CFBundleVersion` em `painel.py` e o número no README quando mudar o pacote.
4. Commit em português, explicando o porquê. Push para `main`.
5. Release com tag `vN.M` e o zip `impera-meta-mcp-vN.zip` (pasta raiz `meta-ads-mcp/`, sem `.venv`, `__pycache__`, `testes/shots`).
6. Dizer ao Marcelo o que mudou e a linha de instalação da versão nova.

## Regras que não mudam

- Somente leitura: o servidor só faz GET na Graph API. Nada que crie, edite, pause ou apague.
- Token só no cabeçalho `Authorization`, nunca em URL, log, resposta de ferramenta ou na janela do app. Nunca pedir token no chat.
- Um token por cliente (`clientes/<id>.env`, permissão 600). Com mais de um cliente, `cliente` é obrigatório: nunca escolher por omissão.
- Nenhum token, `.env`, `clientes/` ou `relatorios/` no repositório.
- O nome do servidor no Claude Desktop é `meta-ads` e a pasta instalada é `~/meta-ads-mcp`. Mudar qualquer um quebra instalações e instruções de projeto já existentes.
- Alcance e frequência são deduplicados: nunca somar entre linhas.
