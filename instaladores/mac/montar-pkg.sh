#!/bin/bash
# Monta o instalador do Mac (dist/Impera-Meta-MCP.pkg) a partir do pacote dist/impera-meta-mcp.zip.
# Só roda num Mac (pkgbuild e productbuild são do macOS); no GitHub Actions, roda no runner macos.
#   bash empacotar.sh && bash instaladores/mac/montar-pkg.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSAO="$(cat VERSAO)"
ZIP="dist/impera-meta-mcp.zip"
[ -f "$ZIP" ] || { echo "Falta $ZIP (rode empacotar.sh antes)."; exit 1; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/scripts" "$T/telas"
cp instaladores/mac/scripts/postinstall "$T/scripts/postinstall"
chmod 755 "$T/scripts/postinstall"
cp "$ZIP" "$T/scripts/impera-meta-mcp.zip"
for f in bemvindo.html concluido.html; do
  sed "s/__VERSAO__/$VERSAO/g" "instaladores/mac/telas/$f" > "$T/telas/$f"
done
sed "s/__VERSAO__/$VERSAO/g" instaladores/mac/distribution.xml > "$T/distribution.xml"

# Pacote sem arquivos para copiar (--nopayload): quem instala é o postinstall, na pasta do usuário.
pkgbuild --nopayload --scripts "$T/scripts" --identifier local.impera.meta-mcp.instalador \
         --version "$VERSAO" "$T/componente.pkg"
productbuild --distribution "$T/distribution.xml" --resources "$T/telas" --package-path "$T" \
             --identifier local.impera.meta-mcp --version "$VERSAO" "dist/Impera-Meta-MCP.pkg"

(cd dist && shasum -a 256 Impera-Meta-MCP.pkg > Impera-Meta-MCP.pkg.sha256)
echo "Instalador da versão $VERSAO:"; ls -l dist/Impera-Meta-MCP.pkg*
