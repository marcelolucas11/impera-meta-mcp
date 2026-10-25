# Novidades

## 6.7.1

Refinamentos da proteção e avisos antes de ativar. Atualize pelo app e feche e abra o Claude (Cmd+Q).

- **Fila de escrita única:** o espaçamento entre escritas na mesma conta agora vale entre todos os processos do servidor (o Claude Desktop pode abrir mais de um), usando um arquivo com trava (`.ritmo.json`, preservado nas atualizações).
- **Bloqueios aparecem no app:** na ficha do cliente, um aviso vermelho diz que a Meta limitou, até que horas o app parou e por quê.
- **Aviso antes de ativar:** `alterar_status` com `ativar` (e `resumo_carteira`) avisa se a conta não está ativa ou se o limite de gastos está acabando ou acabou, antes de você ligar a campanha e ela não entregar.
- **Teste de interface mais estável:** o teste que falhou de vez em quando no GitHub esperava o painel redesenhar; agora espera.
- Testado só contra Meta simulada.

## 6.7.0

Proteção contra bloqueio da Meta, conferência de tudo que o app cria ou muda, e dez ferramentas novas. Atualize pelo app e depois feche e abra o Claude (Cmd+Q).

**Proteção contra limite e bloqueio (o que causou as tempestades de "limite de chamadas")**
- **Não insiste mais.** Antes, ao receber "limite de chamadas" o app tentava de novo 3 vezes, e essas repetições é que levam a Meta ao sinal de abuso (613/1996). Agora ele para na hora e diz até quando.
- **Lê o uso da API** nos cabeçalhos da Meta (app, BM, conta, insights). Perto do limite (70%, 80%, 90%) ele vai ficando mais devagar sozinho; em 100% pausa aquela conta pelo tempo que a Meta pediu.
- **Disjuntor por conta e por cliente:** sinal de abuso (613/1996) para tudo por 60 min; bloqueio temporário (368) para o token por 30 min; 3 limites em 5 min pausam a conta 15 min. A resposta diz "até HH:MM". O prazo fica gravado no Mac (`.limites.json`) e sobrevive a reiniciar o Claude. O limite de um cliente nunca trava outro.
- **Ritmo:** escritas na mesma conta saem uma por vez, com intervalo; no máximo 6 leituras simultâneas por conta, somando todas as ferramentas.
- **`status_limites`:** mostra o uso em %, bloqueios ativos e até que horas. Não chama a Meta.
- **Consultas de resultados:** recusa antes de chamar o que a Meta rejeita (mais de 37 meses; quebras com mais de 13 meses; dia a dia que viraria mais de 120 chamadas). Consulta pesada demais agora vira relatório assíncrono da Meta, em vez de insistir.

**Conferência depois de escrever**
- `criar_campanha`, `criar_conjunto`, `criar_anuncio` e `duplicar` leem o objeto de volta. Se a Meta devolveu algo ATIVO, o app pausa na hora e avisa. Se não conseguir pausar, vem um ALERTA em maiúsculas. (Era o caso das campanhas que apareceram ativas com R$ 1.000 e R$ 700 por dia.)
- `alterar_status` e `alterar_orcamento` conferem se a Meta gravou o valor.
- Falha ambígua ("pode ter sido aplicada"): o app procura na conta, só lendo, um item com esse nome criado nos últimos 15 min e diz se já existe (para não criar em dobro) ou se é seguro tentar de novo.

**Ferramentas novas**
- `consultar_cobranca`: status e motivo da conta, forma de pagamento, saldo, total gasto, limite de gastos (avisa quando está acabando) e faturas da BM.
- `agendar_aumento_orcamento` e `listar_aumentos_orcamento`: aumento temporário do orçamento de uma campanha, que volta ao normal sozinho. Respeita o teto do app.
- `compartilhar_publico` e `listar_compartilhamento_publico`: usa o mesmo público em várias contas do mesmo cliente e da mesma BM. Nunca cruza clientes. (Não existe "descompartilhar": o app não remove nada.)
- `resumo_carteira`: as contas liberadas de UM cliente lado a lado, com totais por moeda. Alcance e frequência não são somados.
- `ver_criativo`: a imagem (ou a capa do vídeo) aparece na conversa, com texto, link, botão e UTMs.
- `criar_anuncios_video_em_lote`: até 20 anúncios de vídeo pausados a partir de links, esperando a Meta processar cada vídeo.
- `editar_utm`: troca as UTMs de anúncios que já existem (cria um criativo idêntico com as UTMs novas e aponta o anúncio para ele). Tem `simular`.

**Segurança**
- **Tokens no Chaveiro do Mac (opcional):** `cofre.py --migrar` move os tokens dos arquivos para o Chaveiro; o arquivo fica só com o aviso `keychain:impera-meta-mcp`. Confere a leitura antes de trocar e tem `--desfazer`. No Terminal: `~/meta-ads-mcp/.venv/bin/python ~/meta-ads-mcp/cofre.py --status`. O app continua aceitando token em arquivo.
- **Varredura de segredos (gitleaks)** a cada envio ao GitHub: se algum token da Meta ou chave for parar no código, a publicação é bloqueada.

Tudo foi testado só contra uma Meta simulada: nenhuma campanha foi criada ou alterada em conta real. Ainda precisa de conferência na conta: cabeçalhos de uso reais, `budget_schedules`, compartilhamento de público, faturas e a leitura do Chaveiro no seu Mac.

## 6.6.0

Criar conjunto agora funciona com o anunciante verificado, que a Meta exige para anunciar no Brasil.

- **Criar conjunto:** novos campos `anunciante_id` e `pagador_id`. A Meta recusava o conjunto com "anunciante ausente" mesmo com o nome em texto (`dsa_beneficiario`), porque para o Brasil ela quer o anunciante verificado pelo ID. Com `anunciante_id` o app envia o anunciante e o pagador como identidade verificada. Sem `pagador_id`, o pagador é o mesmo do anunciante.
- **Nova ferramenta `listar_anunciantes`:** mostra os anunciantes verificados da conta (os que aparecem como "Concluído" na tela do conjunto) com o ID de cada um. Se a Meta não devolver a lista, a resposta explica como achar o ID e usar em `anunciante_id`.
- **Erro de "anunciante ausente"** agora manda usar `listar_anunciantes` e `anunciante_id`.
- `campos_extras` aceita também `regional_regulation_identities`.
- Testado só contra Meta simulada. Ainda precisa de conferência na conta real: o nome do campo de identidades que a Meta devolve na lista de anunciantes é o ponto menos certo. Se a lista vier vazia, o ID pode ser copiado do Gerenciador e passado direto em `anunciante_id`.

## 6.5.1

Correções de problemas achados no primeiro teste na prática, numa conta real.

- **Listar públicos** voltava erro ("'int' object is not subscriptable"): a Meta devolve a data de criação como número. Agora funciona.
- **Listar catálogos** (e tudo que usa catálogo: produtos, feeds, conjuntos de produtos, criativos de catálogo) falhava porque a Meta tirou esse campo da conta de anúncios. Agora o app lê os catálogos da BM do cliente (próprios e compartilhados). Preencha o ID da BM na ficha do cliente para garantir.
- **Criar público de site, de engajamento, de lista e semelhante:** se a Meta recusar o parâmetro `subtype` (erro 2654), o app tenta uma vez sem ele. A recusa vem antes de processar, então nada é criado em dobro.
- **Criar conjunto** agora aceita `dsa_beneficiario` e `dsa_pagador`: a Meta passou a exigir quem se beneficia e quem paga o anúncio. Sem eles, o erro da Meta agora vem com a explicação do que informar.
- **`campos_extras`** (criar e atualizar conjunto) aceita o JSON como objeto, que é como o Claude Desktop entrega. Antes era rejeitado antes de chegar à Meta.
- Testado só contra Meta simulada. Ainda precisa de conferência na conta real: o público de site sem `subtype`, e a leitura dos catálogos pela BM.

## 6.5.0

- **Teste A/B:** `criar_teste_ab` cria o teste da Meta entre 2 a 5 campanhas, conjuntos ou anúncios que já existem (mesmo tipo e mesma conta), dividindo o público em partes iguais. Não cria gasto novo. O resultado aparece em `listar_testes_ab`.
- **Catálogo, agora com escrita:** `criar_catalogo` (recusa um segundo catálogo sem confirmação, porque a Meta recomenda um só), `criar_feed_produtos` (a Meta busca seu arquivo por https), `criar_regra_feed`, `criar_conjunto_produtos`, `criar_produtos` e `atualizar_produtos` (até 25 por vez; preço, estoque, texto, imagem; esconder produto é `visibilidade=hidden`, nada é apagado).
- **Criativos avançados:** `criar_criativo_avancado` faz PAC (mídia diferente por posicionamento, com uma mídia padrão), MMU (várias mídias e textos, a Meta escolhe), carrossel dinâmico de catálogo e criativo com melhorias Advantage+. Só cria o criativo; use o ID em `criar_anuncio`.
- **Pixel:** `criar_pixel`. As regras de eventos por clique ou URL (Events Setup) não existem na API pública da Meta e continuam só na BM ou no código do site.
- **Comentários:** `listar_comentarios` (de anúncio ou post, Facebook ou Instagram), `moderar_comentario` (oculta ou mostra de novo; nunca apaga) e `responder_comentario` (resposta pública como a página).
- **BM:** `listar_usuarios_bm` e `listar_ativos_bm` (só leitura) e `dar_acesso_ativo`, que dá a alguém da BM acesso LIMITADO (analisar, anunciar, rascunhar; na página também criar conteúdo e moderar) a uma conta ou página que o cliente já usa. Exige uma permissão nova e separada, **"Administrar a BM"**, que vem desligada. Não existe controle total, administrador nem remoção de acesso.
- **Benchmark:** os benchmarks de setor da Meta são proprietários e não existem na API pública, então não dá para trazê-los. No lugar entrou `benchmark_interno`: compara cada campanha, conjunto ou anúncio com a mediana da própria conta (CPM, CTR, CPC, CPA, ROAS) e marca quem está acima ou abaixo do normal.
- Testado só contra Meta simulada. Pontos que dependem de confirmação em conta real: formato de PAC e Advantage+, criação de teste A/B e feed de catálogo. Comentários e BM precisam do usuário do sistema atribuído à página e do ID da BM na ficha.

## 6.4.0

- **Leads de formulário instantâneo.** `listar_formularios` mostra os formulários da página; `baixar_leads` baixa os leads (com filtro de data) para um CSV na pasta do cliente, com permissão restrita. A resposta do Claude mostra só a contagem e uma amostra com e-mail e telefone mascarados. `criar_formulario_leads` cria o formulário (nome, e-mail, telefone etc., política de privacidade e botão final, por exemplo para o grupo do WhatsApp). Para isso o usuário do sistema precisa estar atribuído à página com controle total.
- **Regras automáticas da Meta.** `listar_regras` e `criar_regra_automatica`: por exemplo "se o custo por resultado do conjunto passar de R$ 40 nos últimos 3 dias, avisar ou pausar". A regra só avisa ou pausa, nunca aumenta orçamento, e nasce DESLIGADA para você conferir na BM (ligar já na criação ou pausar exige também a permissão "ativar e pausar").
- **API de Conversões.** `enviar_eventos_conversao` manda vendas e leads do servidor para o pixel, por exemplo compras fechadas no WhatsApp. E-mail e telefone viram hash aqui no seu computador e nada sai em texto puro. Aceita até 1.000 eventos, só dos últimos 7 dias, com ID para não duplicar com o pixel do site. Com o código da aba "Testar eventos" da Meta os eventos ficam só no teste; sem o código eles são reais e entram nos relatórios.
- Todas valem para as contas liberadas do cliente (campo `conta`) e registram no histórico sem dados pessoais nem tokens.
- Testado só contra Meta simulada. Os campos das regras (nomes de métricas e valores em centavos) e do formulário são os que a documentação da Meta indica, mas ainda não foram vistos numa conta real: crie primeiro em conta de baixo risco, e a regra nasce desligada justamente por isso.
- Ainda não existe: criar teste A/B, escrita de catálogo e feed, criativos PAC/Advantage+, edição de eventos do pixel, moderação de comentários, administração da BM.

## 6.3.0

- **Mais de uma conta de anúncios por cliente.** Na ficha do cliente, em Conexão, aparece "Outras contas liberadas": marque as contas que o Claude também pode usar além da padrão (por exemplo a CA 02 do mesmo cliente). A lista usa as contas que o token enxerga.
- **Como o Claude usa:** as ferramentas de criação e de apoio (campanha, conjunto, anúncio, criativo, vídeo, públicos, pixels, páginas, mídias, conversões, catálogos, diagnósticos) ganharam o campo `conta`. Sem ele, continua valendo a conta padrão. Para criar na CA 02, o Claude passa o ID dela.
- **Alterar algo que já existe** (ativar/pausar, orçamento, renomear, duplicar, editar) funciona direto pelo ID: se o item for de uma conta liberada, é aceito, sem precisar informar a conta.
- **Segurança igual à anterior:** conta não marcada é recusada antes de enviar qualquer coisa à Meta, o nome da conta não vale para alterar (só o ID numérico), tudo continua nascendo PAUSADO, o teto diário e as permissões do cliente valem para todas as contas liberadas, e o histórico agora anota em qual conta cada alteração foi feita.
- `listar_clientes` ganhou a coluna `contas_liberadas` e `verificar_conexao` lista as extras. As leituras de relatório e insights já aceitavam qualquer conta que o token enxerga e não mudaram.
- Testado só contra Meta simulada. Para usar: atualize pelo app, marque a conta na ficha do cliente e reinicie o Claude (Cmd+Q).

## 6.2.0

- **Catálogos (só leitura):** `listar_catalogos`, `listar_conjuntos_produtos`, `listar_produtos` (com preço, estoque e se a Meta aprovou) e `diagnosticar_catalogo`, que mostra os feeds, a última carga (itens aceitos, inválidos, erros) e os problemas que a Meta aponta. Só vale para catálogos ligados à conta padrão do cliente.
- **Testes A/B (só leitura):** `listar_testes_ab` mostra os testes da conta, período e a divisão entre as células.
- **Nova: `criar_conversao_personalizada`.** Cria uma conversão por trecho de URL (ex.: página de obrigado) no pixel da conta, com evento e valor padrão opcionais. Exige a permissão "criar e duplicar". Nome repetido é recusado e nada é apagado.
- **Impulsionar post do Instagram** continua feito por `criar_anuncio` com `post_instagram_id`: nasce pausado, para você conferir e ativar.
- Ainda não existe: criar teste A/B, criar catálogo ou feed, anúncio dinâmico de catálogo. Testado só contra Meta simulada, como as versões anteriores.

## 6.1.0

- **Mais nove leituras para o Claude investigar uma conta**, todas só de leitura (nada é alterado):
- `diagnosticar_entrega`: mostra anúncios reprovados, em análise ou com problemas, com o motivo que a Meta deu, e conjuntos com aprendizado limitado.
- `log_atividades`: quem mudou o quê na conta e quando. Ajuda a explicar por que o desempenho mudou.
- `comparar_periodos`: dois períodos lado a lado, com a variação percentual de gasto, compras, ROAS, CPA, CPM e CTR por campanha, conjunto ou anúncio.
- `previa_anuncio`: link para ver como o anúncio aparece no Feed, Stories ou Reels.
- `listar_criativos` e `listar_instagram`: criativos da conta e perfis e posts do Instagram, com o ID do post para usar em novos anúncios.
- `biblioteca_anuncios`: pesquisa na Biblioteca de Anúncios da Meta para ver o que os concorrentes estão rodando.
- `diagnosticar_pixel` e `listar_conversoes_personalizadas`: se o pixel está disparando, quais eventos recebeu (e se falta Purchase) e as conversões personalizadas da conta.
- Como na 6.0.0, foram testadas contra uma Meta simulada. A Biblioteca de Anúncios exige que o dono do token tenha a autorização dela na Meta.

## 6.0.0

- **O app agora faz o que um gestor de tráfego faz na BM.** Passou de 19 para 30 ferramentas, cobrindo o que o MCP nativo da Meta faz na criação e edição de campanhas. Tudo continua nascendo PAUSADO, só na conta padrão do cliente, respeitando o teto diário e com registro no histórico.
- **Campanhas completas:** categorias especiais de anúncio (com país), orçamento total (vitalício) com data de fim, limite de gasto, datas de início e fim. Para jogos e apostas, o Claude avisa que a Meta exige autorização da conta.
- **Conjuntos completos:** públicos incluídos e excluídos, interesses, posicionamentos manuais (Feed, Reels, Stories etc.), destinos de site, WhatsApp, Messenger, Direct, formulário instantâneo e perfil do Instagram, meta de otimização escolhida e orçamento total.
- **Públicos novos:** de visitantes do site (todos, por URL, por evento, com exclusão de quem comprou, por tempo no site), de engajamento (Instagram, página, vídeo) e **semelhantes** de 1% a 20%, vários de uma vez e em um ou mais países. Também dá para renomear públicos e adicionar ou remover pessoas de uma lista.
- **Pesquisa:** `buscar_interesses` (com tamanho de público) e `estimar_publico` (alcance estimado antes de criar).
- **Criativos:** carrossel (2 a 10 cartões), vídeo, post existente da página e post orgânico do Instagram, com `criar_criativo` separado do anúncio. `enviar_video` sobe vídeo por link ou direto do seu Mac, em pedaços, até 4 GB.
- **Edição:** `atualizar_conjunto` (segmentação, públicos, posicionamentos, datas, mantendo o que não foi mexido) e `atualizar_campanha` (datas e limite de gasto). A permissão "Renomear" agora se chama "Renomear e editar".
- Importante: estas ferramentas foram testadas contra uma Meta simulada. Formatos específicos (regras de público, semelhantes, formulários) ainda não foram confirmados em conta real, então teste primeiro numa conta de baixo risco.

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
