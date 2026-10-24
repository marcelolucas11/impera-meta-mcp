# Novidades

## 5.1.1

- Corrige `listar_publicos`, que dava erro ao mostrar a data de criação dos públicos (a Meta manda a data como número). Agora lista os públicos da conta com tamanho e status normalmente.

## 5.1.0

- **Públicos de lista de clientes.** O Claude agora cria um público personalizado a partir de um CSV que está no seu Mac e sobe a lista inteira para a Meta, em lotes de 10.000 linhas, na conta padrão do cliente. É a ferramenta `criar_publico_lista`, liberada pela mesma permissão de "criar e duplicar".
- O CSV precisa estar com hash SHA-256 (colunas email, phone, fn, ln, ct, st, country, doby, dobm, dobd). Nada em texto puro sai do Mac, e arquivo sem hash é recusado antes de enviar qualquer coisa.
- **Público baseado em valor.** Se o CSV tem uma coluna `value`, o público é criado baseado em valor e cada linha leva o seu valor, para criar uma Semelhante melhor.
- Antes de enviar, dá para pedir uma **simulação**: o Claude confere o arquivo e mostra quantas linhas serão enviadas, quantas foram descartadas (repetidas, sem email/telefone) e quantos lotes serão.
- Se já existir um público com o mesmo nome, nada é criado. Se o envio parar no meio, o Claude não repete sozinho e diz quantas linhas a Meta já aceitou.
- Nova leitura `listar_publicos`: mostra os públicos da conta com tamanho aproximado e status de processamento.
- Nenhuma ferramenta apaga público. Tudo fica no histórico em "Alterações feitas pelo Claude", sem gravar os dados da lista.

## 5.0.5

- **Nova chave "Exigir período nas análises"**, na ficha de cada cliente (seção "O que o Claude pode fazer"). Ligada, o Claude só consulta dados e gera relatórios com a data inicial e a final que você escolheu. Se você não disser o período, ele pergunta antes, em vez de usar sozinho "últimos 7 dias".
- **Análise só do que veiculou.** Ao puxar a estrutura da conta com um período, o Claude recebe só as campanhas, conjuntos e anúncios que gastaram naquelas datas, inclusive os que foram pausados depois, com o gasto do período e ordenados por gasto. Em contas grandes, a resposta fica muito menor e mais rápida, e para de estourar o limite de chamadas da Meta.
- **Duplicar com orçamento novo.** Agora dá para dizer o orçamento diário das cópias na hora de duplicar. O teto do cliente é conferido contra esse valor novo, não contra o do original. Assim uma campanha de R$ 6.000 pode virar cópias de R$ 300 sem mexer no teto. As cópias continuam nascendo pausadas.
- O teto também passa a valer ao duplicar campanha com orçamento nos conjuntos: nenhum conjunto copiado pode passar do teto.

## 5.0.4

- **Corrige "este ID é desconhecido" / "este ID é de outro tipo"** ao duplicar ou mudar orçamento, limite de CPA e ROAS mínimo, mesmo com o ID certo de campanha ou conjunto. A Meta deixou de informar, em algumas respostas, se o ID é campanha, conjunto ou anúncio (ou informa com nomes antigos). Agora o app descobre o tipo sozinho, consultando o próprio objeto, e duplicar e alterar orçamento voltam a funcionar.
- As mensagens de recusa dizem em português o que o ID é ("este ID é de um anúncio").

## 5.0.3

- **Métricas de vídeo de volta.** `consultar_insights` aceita `com_video` e devolve reproduções, 25%, 50%, 75%, 100% do vídeo e ThruPlays (colunas video_plays, video_p25, video_p50, video_p75, video_p100, thruplays). Esse ajuste existia só na cópia local e se perdeu ao atualizar para a 4.1.1; agora faz parte do app.
- `estrutura_conta` no nível anúncios volta a trazer o tipo do criativo (`tipo_criativo`: VIDEO, SHARE, PHOTO...) e o `video_id`.

## 5.0.2

- Em Ajustes do Sistema > Geral > Itens de Início de Sessão, o segundo plano aparece como **Impera Meta MCP**, com a logo (antes aparecia como "python"). Quem já tinha o segundo plano ligado passa a ver o nome certo depois de atualizar.

## 5.0.1

- O app volta a mostrar a logo da Impera no Dock, no Finder e no Launchpad (na 5.0.0 aparecia o ícone padrão).

## 5.0.0

- **O Claude pode alterar campanhas, com a sua permissão.** Na ficha de cada cliente, em "O que o Claude pode fazer", ligue o que quiser liberar: ativar e pausar, orçamentos e lances, renomear, criar e duplicar. Começa tudo desligado (só leitura).
- Tudo que o Claude cria ou duplica nasce **pausado**. Alterações só acontecem na conta padrão do cliente, respeitam o **teto de orçamento diário** que você definir e ficam registradas em "Alterações feitas pelo Claude".
- Ferramentas novas para o Claude: alterar_status, alterar_orcamento, renomear, duplicar, criar_campanha, criar_conjunto, criar_anuncio (com envio de imagem), listar_pixels, listar_paginas, listar_midias e buscar_localizacao.
- **App de verdade no Dock**: janela própria, com o ponto de "aberto" embaixo do ícone. Clicar no ícone com o app aberto só traz a janela para a frente, sem abrir outra.
- Fechar a janela fecha o app (o ponto some).
- **Manter em segundo plano** (chave no rodapé): ligado, o painel continua ativo com o app fechado, volta sozinho ao ligar o Mac e avisa quando sair versão nova. Desligado, fechar o app encerra tudo. A conexão do Claude com a Meta funciona nos dois casos.
- Para alterações, o token do cliente precisa ter a permissão ads_management; o app avisa quando faltar.

## 4.1.2

- Corrige o "Rota desconhecida" ao buscar atualização logo depois de instalar por cima com o app aberto: o painel antigo continuava rodando. Agora o instalador fecha o painel antigo, e abrir o app sempre sobe o painel da versão instalada.
- Se a janela encontrar um painel de versão anterior, mostra o botão "Concluir atualização".

## 4.1.1

- Ao atualizar pelo app, os instaladores continuam executáveis (antes perdiam a permissão de execução).

## 4.1.0

- Botão "Buscar atualização" no app. Quando há versão nova, mostra o que mudou e atualiza com um clique.
- Atualizar nunca mexe em clientes, tokens, ajustes ou relatórios: só os arquivos do programa são trocados, e a versão anterior fica guardada em `.versoes-anteriores/`.
- O pacote baixado é conferido pelo checksum publicado antes de instalar.
- Instalador de duplo clique ("Instalar Impera Meta MCP") dentro do zip, e instalação por uma linha no Terminal.
- O instalador agora sempre instala em `~/meta-ads-mcp`, de qualquer pasta onde o zip for aberto.

## 4.0.0

- App com o nome Impera Meta MCP e ícone com a logo da Impera.
- Vários clientes, um token por cliente, guardados só no Mac.
- Exportação do relatório com as Tabelas A, B e C do prompt de análise v4.
