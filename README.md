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
| O que o Claude pode fazer | Liga e desliga, por cliente: ativar e pausar, orçamentos e lances, renomear, criar e duplicar. Mais o teto de orçamento diário. |
| Alterações feitas pelo Claude | O registro de cada alteração: quando, o quê, valores antes e depois, e se foi aplicada. |
| Relatórios | Escolher o período e exportar o `.xlsx`; abrir o arquivo ou mostrá-lo no Finder. |
| No projeto do Claude | A instrução pronta para colar no projeto daquele cliente. |
| Rodapé da coluna | Ligação com o Claude Desktop, **Manter em segundo plano**, versão e **Buscar atualização**. |

- **Dock**: o app tem janela própria e mostra o ponto de "aberto" embaixo do ícone. Clicar no ícone com o app aberto só traz a janela para a frente. Fechar a janela fecha o app e o ponto some.
- **Manter em segundo plano**: ligado, o painel continua ativo com o app fechado, volta sozinho ao ligar o Mac e avisa quando sair versão nova. Desligado, fechar o app encerra tudo.
- A conexão do Claude com a Meta não depende do app: o Claude Desktop sobe o servidor sozinho, com o app aberto ou fechado.
- O token é digitado no app e nunca volta a aparecer: nem na janela, nem em log.
- O painel só atende este Mac (127.0.0.1) e exige uma chave aleatória por sessão.
- Cliente novo, token trocado ou permissão alterada vale na hora para o Claude, sem reiniciar.

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
- Para só ler, basta a permissão `ads_read`. Para o Claude alterar campanhas, o token precisa também de `ads_management`, e o usuário do sistema precisa de acesso de anunciante à conta.
- A conta de anúncios precisa estar atribuída ao usuário do sistema (Atribuir ativos).
- A pasta `clientes/` está no `.gitignore`. Não suba para o GitHub e não cole tokens em chat.

## Ferramentas que o Claude ganha

Leitura (sempre disponível):

| Ferramenta | O que faz |
|---|---|
| `listar_clientes` | Clientes configurados, conta padrão, permissões e pasta de relatórios. Nunca mostra tokens. |
| `verificar_conexao` | Testa o token de um cliente, ou de todos. |
| `listar_contas` | Contas de anúncios que o token do cliente enxerga. |
| `estrutura_conta` | Orçamento, estratégia de lance, limite de CPA, ROAS mínimo e público de cada campanha/conjunto. |
| `consultar_insights` | Métricas por conta, campanha, conjunto ou anúncio; por dia ou no período; com quebra por idade/gênero, plataforma, posicionamento, dispositivo, região ou hora. |
| `exportar_relatorio` | Gera o `.xlsx` com as Tabelas A, B e C do prompt v4, conta por dia e estrutura. |
| `listar_pixels`, `listar_paginas`, `listar_midias`, `buscar_localizacao` | Pixels, páginas e Instagram, imagens e vídeos da conta, e chaves de cidades/estados para segmentação. |
| `listar_publicos` | Públicos personalizados da conta, com tamanho aproximado e status. |

Alteração (só com a permissão ligada no app para aquele cliente):

| Ferramenta | Permissão | O que faz |
|---|---|---|
| `alterar_status` | Ativar e pausar | Ativa ou pausa campanhas, conjuntos e anúncios. |
| `alterar_orcamento` | Orçamentos e lances | Orçamento diário/total, limite de CPA e ROAS mínimo, dentro do teto. |
| `renomear` | Renomear | Muda o nome de campanha, conjunto ou anúncio. |
| `duplicar` | Criar e duplicar | Copia campanha (com conjuntos e anúncios), conjunto (com anúncios) ou anúncio. Cópias pausadas. |
| `criar_campanha` | Criar e duplicar | Campanha pausada, com orçamento na campanha ou nos conjuntos. |
| `criar_publico_lista` | Criar e duplicar | Público personalizado a partir de um CSV (com hash SHA-256) do Mac, enviado em lotes; aceita coluna `value` e simulação. |
| `criar_publico_site` | Criar e duplicar | Público de visitantes do site (todos, URL, evento, exclusão, tempo no site). |
| `criar_publico_engajamento` | Criar e duplicar | Público de quem engajou com Instagram, página ou vídeo. |
| `criar_publico_semelhante` | Criar e duplicar | Semelhantes de 1% a 20%, um ou vários, em um ou mais países. |
| `criar_criativo` | Criar e duplicar | Criativo de imagem, vídeo, carrossel, post existente ou post do Instagram, sem criar o anúncio. |
| `enviar_video` | Criar e duplicar | Sobe vídeo por link ou do Mac (em pedaços, até 4 GB). |
| `alterar_publico`, `atualizar_publico_lista` | Renomear e editar | Renomear público; adicionar ou remover pessoas de uma lista. |
| `atualizar_conjunto`, `atualizar_campanha` | Renomear e editar (limite de gasto: Orçamentos) | Editar segmentação, públicos, posicionamentos, datas e limite de gasto. |
| `buscar_interesses`, `estimar_publico` | Leitura | Achar IDs de interesses e estimar o tamanho do público. |
| `diagnosticar_entrega`, `log_atividades`, `comparar_periodos` | Leitura | Reprovações e aprendizado; quem mudou o quê; dois períodos lado a lado. |
| `previa_anuncio`, `listar_criativos`, `listar_instagram` | Leitura | Prévia por posicionamento; criativos da conta; perfis e posts do Instagram. |
| `biblioteca_anuncios`, `diagnosticar_pixel`, `listar_conversoes_personalizadas` | Leitura | Anúncios de concorrentes; saúde do pixel; conversões personalizadas. |
| `listar_catalogos`, `listar_conjuntos_produtos`, `listar_produtos`, `diagnosticar_catalogo` | Leitura | Catálogos, conjuntos de produtos, produtos e saúde dos feeds. |
| `listar_testes_ab` | Leitura | Testes A/B da conta. |
| `criar_conversao_personalizada` | Criar e duplicar | Conversão por trecho de URL, ligada a um pixel da conta. |
| `listar_formularios`, `baixar_leads` | Leitura | Formulários da página e download dos leads em CSV (amostra mascarada). |
| `criar_formulario_leads` | Criar e duplicar | Formulário instantâneo com perguntas, política de privacidade e tela final. |
| `listar_regras` | Leitura | Regras automáticas da conta. |
| `criar_regra_automatica` | Criar e duplicar (+ ativar e pausar se pausa ou liga) | Regra que avisa ou pausa; nasce desligada. |
| `enviar_eventos_conversao` | Criar e duplicar | API de Conversões: vendas e leads do servidor para o pixel, com hash local. |
| `criar_teste_ab` | Criar e duplicar | Teste A/B entre campanhas, conjuntos ou anúncios existentes. |
| `criar_catalogo`, `criar_feed_produtos`, `criar_regra_feed`, `criar_conjunto_produtos`, `criar_produtos` | Criar e duplicar | Escrita de catálogo, feed, regras, conjuntos e produtos. |
| `atualizar_produtos` | Renomear e editar | Preço, estoque e textos de produtos; esconder via visibilidade. |
| `criar_criativo_avancado` | Criar e duplicar | PAC, MMU, catálogo dinâmico e Advantage+. |
| `criar_pixel` | Criar e duplicar | Cria pixel na conta. |
| `listar_comentarios` | Leitura | Comentários de anúncio ou post. |
| `moderar_comentario` | Renomear e editar | Oculta ou mostra comentário (nunca apaga). |
| `responder_comentario` | Criar e duplicar | Resposta pública como a página. |
| `listar_usuarios_bm`, `listar_ativos_bm` | Leitura | Usuários e ativos da BM. |
| `dar_acesso_ativo` | Administrar a BM | Acesso limitado de um usuário da BM a conta ou página. |
| `benchmark_interno` | Leitura | Compara itens com a mediana da própria conta. |
| `criar_conjunto` | Criar e duplicar | Conjunto pausado: evento do pixel, orçamento, lance, cidades/estados, idade, gênero, Advantage+. |
| `criar_anuncio` | Criar e duplicar | Anúncio pausado com criativo existente ou novo (imagem por link, do Mac ou da conta; vídeo da conta). |

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

- Leitura sempre; alteração só com a permissão do cliente ligada no app, só na conta padrão dele e nas contas que ele liberar na ficha, e o Claude Desktop pede confirmação a cada uma.
- Tudo que é criado ou duplicado nasce pausado. Ativar é uma ação separada, que exige a permissão "Ativar e pausar".
- Orçamentos acima do teto do cliente são recusados antes de chegar à Meta. Nenhuma ferramenta apaga nada.
- Uma alteração nunca é reenviada às cegas: se a conexão cair no meio, o Claude avisa que ela pode ter sido aplicada, em vez de repetir.
- O token vai no cabeçalho `Authorization`, nunca em URL, log, histórico ou resposta.

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

Para publicar uma versão: atualize `VERSAO` e `NOVIDADES.md` e envie para a `main`. O GitHub Actions roda os testes e, se essa versão ainda não tiver release, cria a tag, monta o pacote e publica; o app de todo mundo passa a oferecer a atualização.

Os testes não usam token real nem chamam a Meta. Cobrem: isolamento entre clientes, chamadas simultâneas pelo protocolo MCP, exportação com conferência de somas, o app num navegador real e a proteção da API local.

Nunca entram no repositório: `.env`, `clientes/`, `relatorios/`, `.painel.json`, `painel.log` (estão no `.gitignore`).

