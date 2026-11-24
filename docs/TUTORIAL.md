# Tutorial: do download ao primeiro relatório

Guia para quem vai instalar o Impera Meta MCP no próprio computador (Mac ou Windows) e usar com o Claude. Parte do princípio de que você já sabe o que é um MCP, uma BM, um usuário do sistema e um token da Meta. Os detalhes de cada tela estão no [README](../README.md).

---

## Como a conexão funciona

Tudo roda no **seu** computador. Nada passa por servidor da Impera nem por nuvem de terceiros.

```
 Claude Desktop  ──(MCP local, stdio)──▶  servidor meta-ads  ──(HTTPS)──▶  API de Marketing da Meta
 (no seu computador)                     (server.py, na pasta        (com o token do cliente
                                          meta-ads-mcp)              que você pediu)
                                                ▲
                                                │ lê clientes e tokens
                                                │
                                       App Impera Meta MCP
                                       (onde você cadastra clientes,
                                        tokens e permissões)
```

Três peças:

1. **O app**: a janela onde você cadastra clientes, cola o token de cada um e escolhe o que o Claude pode fazer. Ele só **guarda a configuração**.
2. **O servidor `meta-ads`**: o MCP em si. É quem conversa com a Meta. Você não abre o servidor: **o Claude Desktop sobe ele sozinho** sempre que abre.
3. **O Claude Desktop**: sabe que o servidor existe porque o instalador escreveu a entrada `meta-ads` no arquivo de configuração dele (`claude_desktop_config.json`), com backup do que já havia.

O que isso significa na prática:

- **É preciso usar o Claude Desktop**, no mesmo computador onde o app está instalado. O claude.ai no navegador ou no celular não enxerga um MCP que roda na sua máquina, a não ser em conversas ligadas a esse computador pelo Claude Desktop, e com ele aberto e online.
- **O app não precisa ficar aberto** para o Claude puxar dados. Quem precisa estar aberto é o Claude Desktop.
- **Cada pessoa usa os próprios tokens.** Instalar o app em outro computador não traz os clientes de ninguém: a lista começa vazia.
- **Os tokens ficam só no seu computador**, num arquivo por cliente que apenas o seu usuário lê. Eles nunca vão para o GitHub, para o chat ou para os logs.

---

## Passo 1. Antes de instalar

Tenha em mãos:

- **O Claude Desktop instalado** e com login feito.
- **Um token por cliente**, gerado na BM do cliente com um usuário do sistema.
  - Para só ler dados, basta `ads_read`.
  - Para o Claude também alterar campanhas, o token precisa de `ads_management`. Além disso, o usuário do sistema precisa ter acesso de anunciante à conta.
  - A conta de anúncios precisa estar **atribuída** a esse usuário do sistema (Atribuir ativos).
  - Se você quiser usar leads, páginas ou Instagram, inclua as permissões correspondentes no token.

> Prefira um token de usuário do sistema a um token pessoal. Ele não expira junto com a sua sessão no Facebook e pode ser revogado sem afetar o seu perfil.

---

## Passo 2. Baixar e instalar

Na [página do repositório](https://github.com/marcelolucas11/impera-meta-mcp), clique em **Baixar para Mac** ou **Baixar para Windows**.

- **Mac**: abra o `.pkg` e siga as telas. Como o instalador não é assinado pela Apple, na primeira vez você vai precisar liberar em *Ajustes do Sistema > Privacidade e Segurança > Abrir Mesmo Assim*.
- **Windows**: abra o `.exe`. Se aparecer "O Windows protegeu o computador", clique em *Mais informações > Executar assim mesmo*. Não precisa de administrador nem de Python instalado.

O instalador faz quatro coisas:

1. prepara um Python só para o app;
2. instala tudo na pasta `meta-ads-mcp` do seu usuário;
3. liga o servidor `meta-ads` ao Claude Desktop;
4. abre o app.

**Ao terminar, feche o Claude Desktop por completo e abra de novo.** Ele só lê a lista de MCPs quando abre.

- No Mac: Cmd+Q.
- No Windows: ícone do Claude perto do relógio, botão direito, *Sair*.

---

## Passo 3. Conferir se o Claude enxerga o app

Primeiro, olhe o app. No rodapé da coluna da esquerda deve aparecer **Claude Desktop ligado**. Se aparecer *ainda não ligado*, clique em **Ligar ao Claude** e reinicie o Claude de novo.

Depois, olhe o Claude Desktop. Numa conversa nova, o servidor `meta-ads` deve aparecer na lista de ferramentas ou conectores. Também dá para conferir em *Configurações > Desenvolvedor*.

Se o `meta-ads` não aparecer, veja a seção [Se algo falhar](#se-algo-falhar).

---

## Passo 4. Cadastrar os clientes

No app, clique em **Novo cliente** e informe o nome e o token. O app testa o token na hora e mostra as contas de anúncios que ele enxerga.

Depois, na ficha do cliente, em **Conexão**:

- **Conta padrão**: a conta que o Claude usa quando você não especifica outra. As alterações acontecem só nela e nas contas que você marcar como liberadas.
- **Evento de compra**: deixe no automático. Troque só se a conta usar uma conversão personalizada (veja [Se algo falhar](#se-algo-falhar)).
- **Exigir período** (opcional): obriga toda consulta a ter data inicial e final, o que evita relatórios sem período definido.

Cliente novo ou token trocado vale na hora para o Claude, sem reiniciar nada.

---

## Passo 5. Decidir o que o Claude pode fazer

Em **O que o Claude pode fazer**, cada cliente tem as próprias chaves:

| Chave | Libera |
|---|---|
| (sempre ligado) | Leitura: insights, estrutura, relatórios, públicos, criativos, diagnósticos |
| Ativar e pausar | Ligar e desligar campanhas, conjuntos e anúncios |
| Orçamentos e lances | Mudar orçamento, limite de CPA e ROAS mínimo, dentro do **teto diário** que você definir |
| Renomear e editar | Nomes, segmentação, públicos, datas, UTMs |
| Criar e duplicar | Campanhas, conjuntos, anúncios, públicos e criativos (**tudo nasce pausado**) |
| Administrar a BM | Dar acesso limitado a contas e páginas |

Nenhuma ferramenta apaga nada. O Claude Desktop pede confirmação a cada alteração, e todas ficam registradas em **Alterações feitas pelo Claude**.

> Para começar, deixe só a leitura. Libere as outras chaves conforme você ganhar confiança.

---

## Passo 6. Um projeto do Claude por cliente

Com mais de um cliente cadastrado, o Claude precisa saber de qual cliente você está falando. O servidor **nunca escolhe sozinho**.

O jeito mais simples de resolver isso:

1. No Claude Desktop, crie um **projeto** para cada cliente.
2. Na ficha do cliente no app, em **No projeto do Claude**, clique em **Copiar instrução**.
3. Cole o texto nas instruções do projeto.

A partir daí, tudo que você pedir dentro daquele projeto usa só o token e as contas daquele cliente.

---

## Passo 7. Primeiro uso

Dentro do projeto de um cliente, peça coisas como:

- "Verifica a conexão." (confirma o token, as permissões e as contas)
- "Como foram os últimos 7 dias? Investido, compras, ROAS e custo por compra."
- "Mostra a estrutura da conta: campanhas, orçamentos e públicos."
- "Compara esta semana com a anterior por conjunto."
- "Exporta o relatório de 1 a 30 deste mês."

O `.xlsx` vai para a pasta de relatórios daquele cliente e também aparece no app, em **Relatórios**.

---

## Passo 8. Manter atualizado

O app confere sozinho se há versão nova quando abre. Também dá para clicar em **Buscar atualização**, no rodapé da coluna da esquerda.

A atualização troca só os arquivos do programa. Clientes, tokens, relatórios e histórico ficam como estão.

**Depois de atualizar, reinicie o Claude Desktop** para ele subir o servidor novo.

---

## Se algo falhar

| Sintoma | O que fazer |
|---|---|
| O `meta-ads` não aparece no Claude | Clique em **Ligar ao Claude** no app e reinicie o Claude **por completo**. Se continuar, veja o log do servidor no Claude: `~/Library/Logs/Claude/mcp-server-meta-ads.log` (Mac) ou `%APPDATA%\Claude\logs\mcp-server-meta-ads.log` (Windows). |
| O Claude diz que não tem acesso | Confira se você está no **Claude Desktop**, no computador onde o app foi instalado, e não no navegador. |
| "A Meta recusou o token" | O token expirou ou foi revogado. Gere outro na BM e troque na ficha do cliente. |
| "Não tem permissão" | O token não tem `ads_read`, ou a conta não está atribuída ao usuário do sistema. |
| Compras zeradas, mas há vendas no Gerenciador | A conta usa conversão personalizada. Na ficha do cliente, coloque em *Evento de compra* o valor `offsite_conversion.custom.<ID>`. |
| O Claude pergunta qual cliente usar | Você está fora de um projeto de cliente, ou o projeto não tem a instrução do Passo 6. |
| A Meta limitou as chamadas | O app espera sozinho e informa até que horas. Peça ao Claude "status dos limites" para ver. |

Para mais detalhes sobre cada tela, a lista completa de ferramentas e as regras de segurança, veja o [README](../README.md).
