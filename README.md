# Impera Meta MCP (app para Mac, somente leitura, vários clientes)

Um app com janela própria para cadastrar clientes e seus tokens da BM, e um servidor MCP que dá ao Claude Desktop acesso de leitura às contas de anúncios de cada cliente. Tudo roda e fica guardado só neste Mac.

<p align="center">
  <a href="https://github.com/marcelolucas11/impera-meta-mcp/releases/latest/download/impera-meta-mcp.zip"><img src="recursos/botao-baixar-mac.svg" alt="Baixar para Mac" width="340"></a>
</p>

## Instalar no Mac

1. Clique em **Baixar para Mac** acima. O arquivo `impera-meta-mcp.zip` vai para Downloads.
2. Abra o zip (duplo clique) e dê duplo clique em **Instalar Impera Meta MCP**.
3. Na primeira vez, o macOS avisa que não conseguiu verificar o desenvolvedor, porque o app não é assinado pela Apple. Abra **Ajustes do Sistema > Privacidade e Segurança**, role até o aviso do "Instalar Impera Meta MCP" e clique em **Abrir Mesmo Assim**. Depois, dê duplo clique de novo.
4. O instalador faz o resto e abre o app. Na primeira instalação, feche o Claude (Cmd+Q) e abra de novo.

Prefere sem aviso nenhum? Cole esta linha no Terminal; ela baixa a versão mais nova, confere o checksum e instala:

```bash
curl -fsSL https://raw.githubusercontent.com/marcelolucas11/impera-meta-mcp/main/instalar-mac.sh | bash
```

O instalador:

- instala sempre em `~/meta-ads-mcp`, sem apagar clientes, tokens, ajustes ou relatórios que já existam;
- prepara o Python (se o Mac não tiver Python 3.10+, baixa um próprio com o `uv`, sem senha de administrador);
- cria o app **Impera Meta MCP** na pasta Aplicativos (e remove o app com o nome antigo, se existir);
- liga o servidor ao Claude Desktop, com backup do arquivo de configuração;
- abre o app.

Depois disso, abra o app pelo Spotlight, pelo Launchpad ou pela pasta Aplicativos.

## Atualizar

No app, embaixo na coluna da esquerda, clique em **Buscar atualização**. O app também confere sozinho ao abrir. Quando há versão nova, ele mostra o que mudou e o botão **Atualizar agora**.

- Só os arquivos do programa são trocados. Clientes, tokens, ajustes, relatórios e o ambiente Python ficam como estão.
- O pacote é conferido pelo checksum publicado antes de instalar; se não conferir, nada é alterado.
- A versão anterior fica guardada em `~/meta-ads-mcp/.versoes-anteriores/`.
- Depois de atualizar, feche o Claude (Cmd+Q) e abra de novo para ele usar a versão nova do servidor.

Instalar por cima com o botão de download ou com a linha do Terminal também preserva tudo.

## O app

| Onde | O que faz |
|---|---|
| Coluna da esquerda | Um bilhete por cliente. O ponto no canhoto mostra a conexão: dourado conectado, vermelho com erro. |
| Novo cliente | Nome e token. O app testa na hora e lista as contas que o token enxerga. |
| Últimos 7 dias | Investido, compras, ROAS e custo por compra da conta padrão. |
| Conexão | Trocar token, escolher a conta padrão, definir o evento de compra. |
| Relatórios | Escolher o período e exportar o `.xlsx`; abrir o arquivo ou mostrá-lo no Finder. |
| No projeto do Claude | A instrução pronta para colar no projeto daquele cliente. |
| Rodapé da coluna | Se o Claude Desktop já está ligado ao servidor, com botão para ligar. |

- O app abre numa janela do Chrome (ou Edge/Brave) sem barra de endereço. Sem nenhum deles, abre no navegador padrão.
- O token é digitado no app e nunca volta a aparecer: nem na janela, nem em log.
- O painel só atende este Mac (127.0.0.1), exige uma chave aleatória por sessão e se encerra sozinho alguns minutos depois de a janela ser fechada.
- Cliente novo ou token trocado vale na hora para o Claude, sem reiniciar.

## Um projeto do Claude por cliente

Na ficha de cada cliente, copie a instrução de "No projeto do Claude" e cole nas instruções do projeto dele. Exemplo:

> Neste projeto o cliente do Impera Meta MCP é `primos`. Passe `cliente="primos"` em toda chamada do servidor meta-ads e nunca consulte outro cliente aqui.

Assim, "exporta o relatório dos últimos 7 dias" dentro do projeto PRIMOS puxa só os Primos.

## Como os clientes ficam separados

- Cada cliente é um arquivo `~/meta-ads-mcp/clientes/<nome>.env`, com permissão 600.
- Com mais de um cliente, toda consulta do Claude precisa dizer de qual é. O servidor nunca escolhe um por conta própria.
- Uma chamada usa só o token do cliente pedido.
- Os relatórios de cada cliente ficam em pastas separadas: `~/meta-ads-mcp/relatorios/<cliente>/`.

## O token de cada cliente

- Gerado na BM do cliente: Configurações do negócio > Usuários > Usuários do sistema > Gerar token.
- Só a permissão `ads_read`. Assim o próprio token não consegue alterar campanhas.
- A conta de anúncios precisa estar atribuída ao usuário do sistema (Atribuir ativos).
- A pasta `clientes/` está no `.gitignore`. Não suba para o GitHub e não cole tokens em chat.

## Ferramentas que o Claude ganha

| Ferramenta | O que faz |
|---|---|
| `listar_clientes` | Clientes configurados, conta padrão e pasta de relatórios. Nunca mostra tokens. |
| `verificar_conexao` | Testa o token de um cliente, ou de todos. |
| `listar_contas` | Contas de anúncios que o token do cliente enxerga. |
| `estrutura_conta` | Orçamento, estratégia de lance, limite de CPA, ROAS mínimo e público de cada campanha/conjunto. |
| `consultar_insights` | Métricas por conta, campanha, conjunto ou anúncio; por dia ou no período; com quebra por idade/gênero, plataforma, posicionamento, dispositivo, região ou hora. |
| `exportar_relatorio` | Gera o `.xlsx` com as Tabelas A, B e C do prompt v4, conta por dia e estrutura. |

## O arquivo exportado

| Aba | Conteúdo |
|---|---|
| Tabela A - Idade e Genero | dia x anúncio x idade x gênero |
| Tabela B - Plataforma | dia x anúncio x plataforma |
| Tabela C - Campanhas / Conjuntos / Anuncios | período inteiro, sem dia: alcance e frequência reais do período |
| Conta por dia | conta inteira por dia, com alcance diário deduplicado |
| Estrutura - Campanhas / Conjuntos | orçamentos e limites de CPA reais, já em reais |
| Info | cliente, período, evento de compra usado, conferência de somas, fator de superestimação do alcance |

- Cabeçalhos em português, no molde do export do Gerenciador.
- A primeira linha das Tabelas A, B e C-Campanhas é o resumo da conta, com os nomes em branco, como no Gerenciador. Não entra em soma.
- IDs gravados como texto, para o Excel não arredondar os últimos dígitos.

## Segurança

- O servidor MCP e o app só fazem requisições GET à Meta. Não há código que crie, edite, pause ou apague nada.
- O token vai no cabeçalho `Authorization`, nunca em URL, log ou resposta.

## Se algo falhar

- O app não abre: no Terminal, `~/meta-ads-mcp/.venv/bin/python ~/meta-ads-mcp/painel.py --abrir`, e veja `~/meta-ads-mcp/painel.log`.
- O Claude não mostra o servidor: veja `~/Library/Logs/Claude/mcp-server-meta-ads.log`.
- "A Meta recusou o token": expirou ou foi revogado. Troque na ficha do cliente.
- "Não tem permissão": falta `ads_read` no token ou a conta não está atribuída ao usuário do sistema.
- Compras zeradas com vendas no Gerenciador: a conta usa conversão personalizada. Na ficha, ponha em "Evento de compra" o tipo `offsite_conversion.custom.<ID>`.
- Quem preferir o Terminal: `~/meta-ads-mcp/.venv/bin/python ~/meta-ads-mcp/configurar.py` faz o mesmo cadastro.

## Desenvolvimento

| Arquivo | Papel |
|---|---|
| `server.py` | Servidor MCP (stdio) que o Claude Desktop sobe. Somente GET na Marketing API. |
| `painel.py` + `painel.html` | O app: servidor local em 127.0.0.1 e a janela. |
| `configurar.py` | Cadastro de clientes pelo Terminal e funções usadas pelo app. |
| `atualizar.py` | Busca e instala versões novas pelas releases do GitHub, preservando os dados do usuário. |
| `instalar.sh` | Instalador para Mac (também usado pelo duplo clique e pela linha do Terminal). |
| `instalar-mac.sh` | Instalação por uma linha: baixa a release mais nova, confere o checksum e instala. |
| `empacotar.sh` | Monta `dist/impera-meta-mcp.zip` e o `.sha256` da release. |
| `VERSAO`, `NOVIDADES.md` | Versão atual e o que mudou em cada uma (vira a nota da release e aparece no app). |
| `recursos/` | Ícone (logo Impera) e a fonte Montserrat (licença OFL). |
| `testes/` | Testes contra uma API da Meta simulada. |

Antes de publicar qualquer mudança:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
bash testes/rodar.sh
```

Para publicar uma versão: atualize `VERSAO` e `NOVIDADES.md`, faça o commit e crie a tag `vX.Y.Z`. O GitHub Actions roda os testes, monta o pacote e publica a release; o app de todo mundo passa a oferecer a atualização.

Os testes não usam token real nem chamam a Meta. Cobrem: isolamento entre clientes, chamadas simultâneas pelo protocolo MCP, exportação com conferência de somas, o app num navegador real e a proteção da API local.

Nunca entram no repositório: `.env`, `clientes/`, `relatorios/`, `.painel.json`, `painel.log` (estão no `.gitignore`).

