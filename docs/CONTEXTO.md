# Impera Meta MCP: contexto completo do projeto

Documento de passagem para continuar o projeto em qualquer conversa ou projeto do Claude. Estado em 19/10/2026, versão **5.0.2**.

- **Repositório:** https://github.com/marcelolucas11/impera-meta-mcp (público)
- **Dono:** Marcelo (Impera Services). O sócio também usa, no MacBook dele, com os clientes dele.
- **Regra de trabalho:** toda mudança é feita no repositório primeiro, testada e publicada como versão nova; o app de cada um se atualiza pelo botão "Buscar atualização".
- **Instruções para o Claude** que mexer no código: `CLAUDE.md`, na raiz do repositório.

## 1. O que é

Um app para Mac com duas partes:

| Parte | Arquivo | Função |
|---|---|---|
| Servidor MCP `meta-ads` | `server.py` | O Claude Desktop sobe este servidor sozinho. Ele dá ao Claude acesso à Marketing API da Meta de cada cliente, cada um com o próprio token. |
| App Impera Meta MCP | `painel.py` + `painel.html` + `recursos/app.applescript` | Janela no Dock para cadastrar clientes e tokens, escolher conta padrão e permissões, exportar relatórios, ver o histórico de alterações, ligar o segundo plano e atualizar. |

A ponte do Claude com a Meta é o servidor, não o app: funciona com o app aberto ou fechado.

## 2. Por que foi construído assim (decisões)

| Decisão | Motivo |
|---|---|
| MCP próprio com token de usuário do sistema da BM, em vez do conector do Meta Ads | O Marcelo quer usar tokens das BMs dos clientes (cada cliente cria um app/usuário do sistema e gera o token). O conector nativo só enxerga contas compartilhadas com o perfil logado. |
| Servidor roda no Mac, não na nuvem | O ambiente de nuvem do Claude não alcança `graph.facebook.com` (bloqueado pela rede). Uma skill sozinha também não resolveria pelo mesmo motivo. |
| Token nunca no chat | Token colado em conversa fica no histórico. O token é digitado só no app (ou no Terminal) e fica em arquivo local com permissão 600. |
| Um arquivo por cliente (`clientes/<id>.env`) | Vários clientes, vários tokens; isolamento por chamada: uma chamada usa só o token do cliente pedido. Com mais de um cliente, `cliente` é obrigatório. |
| Projeto do Claude por cliente | Cada projeto recebe uma instrução fixa ("o cliente do Impera Meta MCP é X"), copiada da ficha do cliente no app. |
| Repositório público | Com repositório privado, o app precisaria de token do GitHub em cada Mac para buscar atualização. Nenhum token ou dado de cliente vai para o GitHub. |
| Publicação pelo GitHub Actions | O proxy da sessão do Claude não deixa criar repositório nem enviar tags. O workflow cria a tag e a release sozinho quando a `VERSAO` da `main` ainda não tem release. |
| App nativo via `osacompile` | Não dá para compilar app de Mac fora do Mac. O instalador monta o app no próprio Mac com o `osacompile` (AppleScriptObjC + janela WebKit). Sem `osacompile`, cai num lançador que abre o navegador. |
| Escrita com permissão por cliente | O Marcelo quer usar o app como ponte também para criar e modificar campanhas, escolhendo o que liberar por cliente. |

## 3. Versões

| Versão | O que mudou |
|---|---|
| v1 | Servidor MCP somente leitura, um token, instalador pelo Terminal. Exporta o `.xlsx` com as Tabelas A, B e C do prompt de análise v4. |
| v1 (correção) | Mac sem Python 3.10+: o instalador passa a baixar um Python próprio com o `uv`. |
| v2 | Vários clientes, um token cada, cadastro pelo `configurar.py`. |
| v3 | App visual: painel local em 127.0.0.1 com janela do Chrome, cadastro de cliente, teste de token, conta padrão, últimos 7 dias, exportação. |
| 4.0.0 | Nome Impera Meta MCP e ícone com a logo da Impera. |
| 4.1.0 | Botão Buscar atualização (preserva clientes e tokens), botão Baixar para Mac no GitHub, instalador de duplo clique, instalação por uma linha, publicação automática. |
| 4.1.1 | Atualização mantém os instaladores executáveis. |
| 4.1.2 | Corrige "Rota desconhecida": instalar por cima com o app aberto deixava o painel antigo rodando. |
| 5.0.0 | Permissões de alteração por cliente e 7 ferramentas de escrita; app nativo no Dock (ponto de aberto, uma janela só, fechar = sair); Manter em segundo plano (LaunchAgent). |
| 5.0.1 | Ícone: o `osacompile` usava o ícone padrão do `Assets.car` (`CFBundleIconName`); agora é a logo. |
| 5.0.2 | Em Itens de Início de Sessão o segundo plano aparece como Impera Meta MCP (antes "python"): o serviço roda de dentro do `.app`. |
| 5.0.3 | Métricas de vídeo em `consultar_insights` (`com_video`) e tipo do criativo em `estrutura_conta`. |
| 5.0.4 | Tipo do ID (campanha/conjunto/anúncio) descoberto sem depender do `metadata` da Meta, que vinha vazio ou com nomes antigos e travava `duplicar` e `alterar_orcamento`. |
| 5.0.5 | Chave por cliente "Exigir período" (`EXIGIR_PERIODO=1`); `estrutura_conta` com `desde`/`ate` lista só o que veiculou no período (ativo ou pausado, com `gasto_periodo`); `duplicar` com `orcamento_diario` da cópia (teto conferido contra o valor novo) e teto aplicado aos conjuntos de campanha sem orçamento de campanha. |
| 6.2.0 | `criar_conversao_personalizada`; leituras de catálogo (`listar_catalogos`, `listar_conjuntos_produtos`, `listar_produtos`, `diagnosticar_catalogo`) e `listar_testes_ab`. Impulsionar post = `criar_anuncio` com `post_instagram_id`. Faltam: criar teste A/B, criar catálogo/feed, anúncio dinâmico. Nomes de campos de ad_studies e diagnostics do catálogo não conferidos na Meta real. |
| 6.3.0 | Várias contas por cliente: `CONTAS_LIBERADAS` no `clientes/<id>.env` (marcadas na ficha, em Conexão), parâmetro `conta` (ID numérico) nas ferramentas de criação e apoio, itens existentes aceitos se forem de conta liberada, histórico anota a conta. Só testado contra Meta simulada. |
| 6.1.0 | Nove leituras: `diagnosticar_entrega`, `log_atividades`, `comparar_periodos`, `previa_anuncio`, `listar_criativos`, `listar_instagram`, `biblioteca_anuncios`, `diagnosticar_pixel`, `listar_conversoes_personalizadas`. Faltam: A/B, impulsionar post, criar conversão personalizada, catálogos. |
| 6.0.0 | Auditoria contra o MCP nativo: campanha/conjunto completos (categorias especiais, orçamento total, públicos, interesses, posicionamentos, destinos WhatsApp/formulário), públicos de site/engajamento/semelhante, criativos (carrossel, post, Instagram), `enviar_video`, `atualizar_conjunto/campanha`, `buscar_interesses`, `estimar_publico`. Só testado em API simulada. Faltam (6.1.0): diagnóstico de entrega, prévias, biblioteca de anúncios, pixel/conversões, A/B, catálogos. |
| 5.1.0 | `criar_publico_lista` (CSV com hash SHA-256 do Mac vira público personalizado, em lotes de 10.000, com valor opcional e simulação) e `listar_publicos`. |

## 4. Ferramentas que o Claude tem

Leitura (sempre):

| Ferramenta | O que faz |
|---|---|
| `listar_clientes` | Clientes, conta padrão, permissões, teto, pasta de relatórios. Nunca mostra token. |
| `verificar_conexao` | Testa o token de um cliente ou de todos. |
| `listar_contas` | Contas que o token enxerga. |
| `estrutura_conta` | Campanhas/conjuntos/anúncios com orçamento, estratégia, limite de CPA, ROAS mínimo, público. Com `desde`/`ate`, só o que veiculou no período (ativo ou pausado), com `gasto_periodo`; sem período, pelo status de agora. |
| `consultar_insights` | Métricas por nível, por dia ou período, com quebras (idade/gênero, plataforma, posicionamento, dispositivo, região, hora). |
| `exportar_relatorio` | `.xlsx` com Tabela A (dia x anúncio x idade x gênero), B (dia x anúncio x plataforma), C (sem dia: alcance real do período), conta por dia, estrutura e aba Info com conferência de somas. |
| `listar_pixels`, `listar_paginas`, `listar_midias`, `buscar_localizacao` | Apoio para montar campanhas. |
| `listar_publicos` | Públicos personalizados da conta (tamanho e status). |

Alteração (só com a permissão ligada para o cliente):

| Ferramenta | Permissão (`PERMISSOES`) |
|---|---|
| `alterar_status` | `status` |
| `alterar_orcamento` (orçamento, limite de CPA, ROAS mínimo) | `orcamento` |
| `renomear` | `editar` |
| `duplicar`, `criar_campanha`, `criar_conjunto`, `criar_anuncio`, `criar_publico_lista`, `criar_publico_site`, `criar_publico_engajamento`, `criar_publico_semelhante`, `criar_criativo`, `enviar_video`, `criar_conversao_personalizada` | `criar` |
| `alterar_publico`, `atualizar_publico_lista`, `atualizar_conjunto`, `atualizar_campanha` | `editar` (limite de gasto: `orcamento`) |

Análises: com `EXIGIR_PERIODO=1` no cliente, `consultar_insights` e `exportar_relatorio` recusam chamada sem `desde` e `ate`, e a mensagem manda o Claude perguntar o período (ou usar o que o usuário já disse). `estrutura_conta` sem período continua liberada, porque serve para achar IDs antes de alterar.

Garantias da escrita: só na conta padrão do cliente e nas contas liberadas por ele no app (`CONTAS_LIBERADAS`); tudo criado ou duplicado nasce PAUSADO; teto de orçamento diário (`ORCAMENTO_MAX_DIA`) recusado antes de chegar à Meta; registro em `historico/<cliente>.jsonl` (o app mostra); POST nunca repetido às cegas (só quando a Meta recusou antes de processar); nada é apagado. O token precisa de `ads_management` para alterar.

## 5. Onde ficam as coisas no Mac

Tudo em `~/meta-ads-mcp`:

| Caminho | Conteúdo | Vai para o GitHub? |
|---|---|---|
| `clientes/<id>.env` | Token, conta padrão, contas liberadas, evento de compra, permissões, teto, exigir período (permissão 600) | Nunca |
| `historico/<id>.jsonl` | Alterações feitas pelo Claude | Nunca |
| `relatorios/<id>/` | `.xlsx` exportados | Nunca |
| `.env` | Ajustes gerais (versão da API, pasta de saída, `SEGUNDO_PLANO`) | Nunca |
| `.venv/` | Python do app | Nunca |
| `.versoes-anteriores/` | Cópia da versão anterior a cada atualização | Nunca |
| Programa (`server.py`, `painel.py`, etc.) | Código | Sim |

Fora da pasta: o app em `/Applications/Impera Meta MCP.app` (ou `~/Applications`), o registro do servidor em `~/Library/Application Support/Claude/claude_desktop_config.json` (nome `meta-ads`) e, com segundo plano ligado, `~/Library/LaunchAgents/local.impera.meta-mcp.plist`.

Atualização e reinstalação nunca escrevem nos itens marcados "Nunca".

## 6. Estado atual

- **Mac do Marcelo:** MacBook Air Apple Silicon. Cliente cadastrado: Sort dos Primos (`sort-dos-primos`), conta CA 01 PRIMOS (4031218843828590). O token tem `ads_read`, `ads_management` e `business_management`. Segundo plano ligado.
- **Mac do sócio:** app instalado, com os clientes dele.
- **Marketing API:** v26.0.
- **Testes:** 169 verificações, todas contra uma API da Meta simulada (`bash testes/rodar.sh`), rodando também no GitHub Actions a cada envio.

## 7. O que ainda não foi verificado

- **Alterações contra a Meta de verdade:** `alterar_status` e `alterar_orcamento` já rodaram na Sort dos Primos (20/10/2026, depois da 5.0.4). `duplicar` com `orcamento_diario`, o filtro por veiculação (`filtering` por `id IN` nos edges campaigns/adsets/ads, com plano B de filtrar localmente) e as ferramentas de criação ainda só rodaram contra a simulação.
- **Partes que só existem no Mac:** janela nativa, ícone no Dock, segundo plano com nome e logo em Itens de Início de Sessão. O Marcelo confirmou o app rodando e pediu o ícone (5.0.1) e o nome no segundo plano (5.0.2); falta confirmar o resultado da 5.0.2.
- **"Item de desenvolvedor não identificado":** só some com o app assinado e notarizado com Apple Developer ID (conta paga). Hoje o app é assinado localmente (ad-hoc).

## 8. Ideias e pedidos em aberto

- Skill que exporta o relatório, roda o prompt de análise v4 e publica o painel do cliente.
- Painel (dash) do Sorteio dos Primos atualizado toda segunda às 00:01 com a semana anterior, como tarefa agendada, no molde do painel da Dona Coelha.
- Preferência do Marcelo: instalar apps novos no SSD externo (`/Volumes/SSD-MARCELO/Aplicativos`). O Impera Meta MCP hoje vai para a pasta Aplicativos do Mac; mudar isso exige cuidado porque o servidor do Claude e o segundo plano precisam do disco montado.

## 9. Como continuar numa conversa nova

1. Anexar o repositório `marcelolucas11/impera-meta-mcp` à conversa (o Claude lê o `CLAUDE.md` com o fluxo e as regras).
2. Pedir a mudança. O Claude aplica, roda `bash testes/rodar.sh`, sobe `VERSAO` e `NOVIDADES.md`, e envia para a `main`.
3. O GitHub Actions testa e publica a release.
4. No Mac: Buscar atualização, Atualizar agora; depois fechar o Claude (Cmd+Q) e abrir de novo.

Para instalar do zero num Mac: botão "Baixar para Mac" no README do repositório, ou:

```bash
curl -fsSL https://raw.githubusercontent.com/marcelolucas11/impera-meta-mcp/main/instalar-mac.sh | bash
```
