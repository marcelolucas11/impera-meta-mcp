#!/bin/bash
# Instala ou atualiza o Impera Meta MCP no Mac com a versão mais nova publicada.
#   curl -fsSL https://raw.githubusercontent.com/marcelolucas11/impera-meta-mcp/main/instalar-mac.sh | bash
# Clientes, tokens e relatórios que já existam no Mac ficam como estão.
set -euo pipefail

principal() {
  local url="${IMPERA_URL_PACOTE:-https://github.com/marcelolucas11/impera-meta-mcp/releases/latest/download/impera-meta-mcp.zip}"
  IMPERA_TMP="$(mktemp -d)"
  trap 'rm -rf "${IMPERA_TMP:-}"' EXIT
  local tmp="$IMPERA_TMP"
  echo "Baixando a versão mais nova do Impera Meta MCP..."
  curl -fsSL "$url" -o "$tmp/pacote.zip"
  curl -fsSL "$url.sha256" -o "$tmp/pacote.zip.sha256"
  local esperado obtido
  esperado="$(awk '{print $1}' "$tmp/pacote.zip.sha256")"
  if command -v shasum >/dev/null 2>&1; then obtido="$(shasum -a 256 "$tmp/pacote.zip" | awk '{print $1}')"
  else obtido="$(sha256sum "$tmp/pacote.zip" | awk '{print $1}')"; fi
  if [ "$esperado" != "$obtido" ]; then
    echo "O arquivo baixado não confere com o checksum publicado. Nada foi instalado; tente de novo."
    exit 1
  fi
  unzip -q "$tmp/pacote.zip" -d "$tmp"
  bash "$tmp/impera-meta-mcp/instalar.sh" < /dev/null
}

principal "$@"   # o script inteiro é lido antes de rodar, mesmo vindo por "curl | bash"
