#!/bin/bash
# Roda todos os testes contra uma API da Meta simulada (nenhum token real, nenhuma chamada à Meta).
# Uso, na raiz do repositório:  bash testes/rodar.sh
# Requer: pip install -r requirements.txt -r requirements-dev.txt
# CHROMIUM_PATH aponta para um Chromium já instalado, se o Playwright não tiver o dele.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
echo "== servidor MCP: vários clientes, isolamento de tokens, exportação =="
"$PY" testes/test_multi.py .
echo "== protocolo MCP via stdio: chamadas simultâneas de clientes diferentes =="
"$PY" testes/test_stdio_multi.py "$PY" testes/wrap_server.py .
echo "== app (painel) num navegador real =="
PYTHONPATH=testes "$PY" testes/test_painel.py "$PY" . testes
