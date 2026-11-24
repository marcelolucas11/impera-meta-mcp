# Impera Meta MCP: instruções para o Claude

Repositório do app Impera Meta MCP (Mac) e do servidor MCP `meta-ads`. O dono é o Marcelo (Impera Services). Ele pede mudanças na conversa e o Claude aplica, testa e publica aqui.

História, decisões, estado atual e pendências: `docs/CONTEXTO.md`.

## Fluxo de cada atualização

1. Aplicar a mudança.
2. Rodar `bash testes/rodar.sh` (com `CHROMIUM_PATH` se o Playwright não tiver Chromium próprio). Só publicar com tudo passando; teste novo para comportamento novo.
3. Subir a versão em `VERSAO` (X.Y.Z) e escrever a seção `## X.Y.Z` no topo de `NOVIDADES.md`, em português e do ponto de vista de quem usa o app. Essa seção vira a nota da release e aparece no app em "O que mudou".
4. Commit em português, explicando o porquê. Push para `main`.
5. O push na `main` basta: o GitHub Actions (`.github/workflows/publicar.yml`) roda os testes (Linux e uma instalação de verdade no Windows) e, se a `VERSAO` ainda não tiver release, cria a tag `vX.Y.Z`, monta `impera-meta-mcp.zip` + `.sha256` com `empacotar.sh`, o `Impera-Meta-MCP.pkg` num runner macOS (`instaladores/mac/montar-pkg.sh`, instalado e conferido no próprio runner) e o `Impera-Meta-MCP-Instalador.exe` num runner Windows (Inno Setup, `instaladores/windows/`, instalado em modo silencioso e conferido), cada um com `.sha256`, e publica a release como "latest". Não criar tag à mão (o proxy da sessão não permite enviar tags).
6. Conferir que a release saiu com os seis arquivos (zip, pkg e exe, cada um com o `.sha256`). A partir daí o botão "Buscar atualização" do app de todo mundo oferece a versão nova.
7. Dizer ao Marcelo o que mudou e lembrar: atualizar pelo app e reiniciar o Claude (Cmd+Q).

## Atualização sem perda de dados

- `atualizar.py` e `instalar.sh` só escrevem arquivos do programa. Nunca tocam em `clientes/`, `historico/`, `.env`, `relatorios/`, `.venv/`, `.painel.json`, `painel.log`, `.aviso-versao`, `.versoes-anteriores/`.
- Qualquer arquivo novo de dados do usuário precisa entrar nessas duas listas de preservação (e no `.gitignore`) antes de existir.
- O nome do pacote (`impera-meta-mcp.zip`) e a pasta raiz dele (`impera-meta-mcp/`) não mudam: o "Buscar atualização" dos apps instalados e os dois instaladores dependem deles.
- Os botões do README baixam `Impera-Meta-MCP.pkg` (Mac) e `Impera-Meta-MCP-Instalador.exe` (Windows) da release "latest"; esses nomes também não mudam.
- `instalar-windows.ps1` tem a mesma lista de preservação do `instalar.sh`; arquivo novo de dados do usuário entra nas três listas.

## Regras que não mudam

- Leitura sempre. Escrita só pelas ferramentas de alteração e só quando o cliente tem a permissão ligada no app (`PERMISSOES` no `clientes/<id>.env`: status, orcamento, editar, criar). Sempre só na conta padrão do cliente, tudo criado PAUSADO, respeitando `ORCAMENTO_MAX_DIA`, com registro em `historico/<id>.jsonl`. Nenhuma ferramenta apaga nada.
- POST na Graph API nunca é repetido às cegas: só quando a Meta recusou antes de processar (limite de chamadas, falha de conexão). Em falha ambígua, avisar que pode ter sido aplicada.
- Token só no cabeçalho `Authorization`, nunca em URL, log, resposta de ferramenta ou na janela do app. Nunca pedir token no chat.
- Um token por cliente (`clientes/<id>.env`, permissão 600). Com mais de um cliente, `cliente` é obrigatório: nunca escolher por omissão.
- Nenhum token, `.env`, `clientes/` ou `relatorios/` no repositório.
- O repositório é público: nada de nome real de cliente, ID real de conta, página, pixel ou BM, nem em testes, exemplos ou `docs/`. Nos testes, usar clientes fictícios (`alfa`, `betaco`) e IDs falsos. Esses detalhes ficam no projeto privado do Claude.
- Segredo falso em teste só com os formatos aceitos por `testes/test_segredos.py` e pelo `.gitleaks.toml` (ex.: `EAAFAKE…`, `TOKEN_…`). A pasta `testes/` é varrida como o resto.
- Commits nunca com e-mail pessoal: usar o e-mail `noreply` (do GitHub ou o do Claude).
- O nome do servidor no Claude Desktop é `meta-ads` e a pasta instalada é `~/meta-ads-mcp`. Mudar qualquer um quebra instalações e instruções de projeto já existentes.
- Alcance e frequência são deduplicados: nunca somar entre linhas.

## App do Windows

- Mesma pasta (`%USERPROFILE%\meta-ads-mcp`), mesmo `server.py` e mesmo painel. `painel.py --instalar-app` cria atalhos no Menu Iniciar e na Área de Trabalho (PowerShell + WScript.Shell) para `pythonw.exe -X utf8 painel.py --abrir`, que abre o painel numa janela `--app` do Chrome/Edge/Brave.
- O Claude Desktop sobe o servidor com `python.exe -X utf8 server.py`. O registro vai para `%APPDATA%\Claude` e também para `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude` (Claude instalado como MSIX).
- Processos em separado no Windows usam `CREATE_NO_WINDOW` (`_em_separado()` no painel); a fila de escritas usa `msvcrt` no lugar do `fcntl`.
- Ainda só no Mac: segundo plano (launchd), cofre no Chaveiro, avisos de versão nova pela Central de Notificações.
- `testes/test_windows.py` confere uma instalação real; roda no job `windows` do Actions.

## App do Mac

- `painel.py --instalar-app` / `--pos-atualizacao` monta o `.app` com `osacompile` a partir de `recursos/app.applescript` (janela WKWebView própria, ponto no Dock, `on reopen` traz a janela, fechar = sair). Sem `osacompile`, cai no lançador que abre o navegador.
- Segundo plano = LaunchAgent `local.impera.meta-mcp` (`~/Library/LaunchAgents`), com `--servir --reaproveitar`. Sob o launchd (`IMPERA_LAUNCHD=1`), encerrar/atualizar sai com código 75 para o launchd subir o código novo na mesma porta e chave.
- A ponte do Claude com a Meta é o `server.py`, que o Claude Desktop sobe sozinho; não depende do app estar aberto.

