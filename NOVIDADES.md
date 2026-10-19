# Novidades

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
