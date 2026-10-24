#!/bin/bash
# Roda todos os testes contra uma API da Meta simulada (nenhum token real, nenhuma chamada à Meta).
# Uso, na raiz do repositório:  bash testes/rodar.sh
# Requer: pip install -r requirements.txt -r requirements-dev.txt
# CHROMIUM_PATH aponta para um Chromium já instalado, se o Playwright não tiver o dele.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
mkdir -p testes/shots
falhou=0

rodar() {  # rodar "<título>" comando...
  local titulo="$1"; shift
  echo "== $titulo =="
  local saida; saida="$(mktemp)"
  "$@" 2>&1 | tee "$saida"
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    falhou=1
    # No GitHub Actions, as falhas viram anotações legíveis pela API (o log completo fica no armazenamento do GitHub).
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
      grep -E "^FAIL|Error|Traceback|assert" "$saida" | head -15 | while IFS= read -r linha; do
        echo "::error title=$titulo::${linha:0:900}"
      done
      tail -5 "$saida" | while IFS= read -r linha; do echo "::warning title=$titulo (fim da saída)::${linha:0:900}"; done
    fi
  fi
  rm -f "$saida"
}

rodar "servidor MCP: vários clientes, isolamento de tokens, exportação" "$PY" testes/test_multi.py .
rodar "protocolo MCP via stdio: chamadas simultâneas de clientes diferentes" "$PY" testes/test_stdio_multi.py "$PY" testes/wrap_server.py .
rodar "app (painel) num navegador real" env PYTHONPATH=testes "$PY" testes/test_painel.py "$PY" . testes
rodar "atualização: dados do usuário preservados, pacotes ruins recusados, instaladores" "$PY" testes/test_atualizacao.py .
rodar "alterações com permissão: teto, pausado, conta padrão, histórico, sem repetição" "$PY" testes/test_escrita.py .
rodar "ferramentas avançadas: campanha/conjunto completos, públicos, criativos, vídeo, edição" "$PY" testes/test_avancado.py .
rodar "leituras: diagnóstico, log, prévia, criativos, Instagram, biblioteca, pixel, conversões, comparação" "$PY" testes/test_leituras.py .
rodar "públicos de lista: lotes, valor, simulação, arquivo sem hash, falha no meio" "$PY" testes/test_publicos.py .
rodar "app: uma instância, fechar, segundo plano, launchd, permissões na interface" "$PY" testes/test_app.py .
exit $falhou
