#!/bin/bash
# Duplo clique para instalar ou atualizar o Impera Meta MCP.
# Clientes, tokens e relatórios que já existam no Mac ficam como estão.
cd "$(dirname "$0")" || exit 1
bash ./instalar.sh < /dev/null
echo
echo "Pronto. Pode fechar esta janela."
