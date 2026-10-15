#!/bin/bash
# Monta o pacote de distribuição: dist/impera-meta-mcp.zip e dist/impera-meta-mcp.zip.sha256
# Só entram os arquivos do programa. Nada de clientes, tokens, relatórios ou ambiente Python.
set -euo pipefail
cd "$(dirname "$0")"
VERSAO="$(cat VERSAO)"
rm -rf dist && mkdir -p dist/impera-meta-mcp
for item in server.py painel.py painel.html configurar.py atualizar.py instalar.sh "Instalar Impera Meta MCP.command" \
            requirements.txt .env.example VERSAO README.md NOVIDADES.md recursos; do
  cp -R "$item" dist/impera-meta-mcp/
done
chmod +x dist/impera-meta-mcp/instalar.sh "dist/impera-meta-mcp/Instalar Impera Meta MCP.command"
(cd dist && TZ=UTC find impera-meta-mcp -exec touch -t 202601010000 {} + && zip -qrX impera-meta-mcp.zip impera-meta-mcp)
(cd dist && sha256sum impera-meta-mcp.zip > impera-meta-mcp.zip.sha256)
rm -rf dist/impera-meta-mcp
echo "Pacote da versão $VERSAO:"; ls -l dist
