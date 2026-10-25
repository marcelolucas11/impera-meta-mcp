#!/bin/bash
# Instala ou atualiza o Impera Meta MCP em ~/meta-ads-mcp.
# Copia só os arquivos do programa: clientes, tokens, ajustes e relatórios que já existam ficam intactos.
set -e
cd "$(dirname "$0")"
ORIGEM="$(pwd -P)"
DESTINO="$HOME/meta-ads-mcp"

if [ "$ORIGEM" != "$(cd "$DESTINO" 2>/dev/null && pwd -P)" ]; then
  echo "Instalando em $DESTINO..."
  mkdir -p "$DESTINO"
  for item in "$ORIGEM"/* "$ORIGEM"/.[!.]*; do
    [ -e "$item" ] || continue
    nome="$(basename "$item")"
    case "$nome" in
      # O que é do usuário (ou não faz parte do programa) nunca é copiado por cima.
      .env|.limites.json|clientes|relatorios|historico|.venv|.painel.json|painel.log|.aviso-versao|.versoes-anteriores|.git|.github|testes|__pycache__) continue ;;
    esac
    cp -R "$item" "$DESTINO/"
  done
  cd "$DESTINO"
fi

# Arquivos vindos de um download carregam a marca de quarentena do macOS; os nossos não precisam dela.
if command -v xattr >/dev/null 2>&1; then xattr -dr com.apple.quarantine "$DESTINO" 2>/dev/null || true; fi

versao_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

if [ -x .venv/bin/python ] && versao_ok .venv/bin/python; then
  echo "Ambiente Python já existe ($(.venv/bin/python --version))."
else
  rm -rf .venv
  PY=""
  if [ -z "$FORCAR_UV" ]; then
    for c in python3.13 python3.12 python3.11 python3.14 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
      if command -v "$c" >/dev/null 2>&1 && versao_ok "$c"; then PY="$c"; break; fi
    done
  fi
  if [ -n "$PY" ]; then
    echo "Usando $("$PY" --version) em $(command -v "$PY")"
    "$PY" -m venv .venv
  else
    # Sem Python 3.10+ na máquina: o uv baixa um Python próprio, sem senha de administrador.
    echo "Não achei Python 3.10 ou mais novo. Baixando um com o uv..."
    UV="$(command -v uv || true)"
    if [ -z "$UV" ]; then
      curl -LsSf https://astral.sh/uv/install.sh | sh
      UV="$HOME/.local/bin/uv"
    fi
    "$UV" venv --seed --python 3.13 .venv
  fi
fi

echo "Instalando dependências..."
.venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo "Versão instalada: $(cat VERSAO 2>/dev/null || echo '?')"
# Se o app estiver aberto com a versão anterior, fecha o painel para ele voltar com o código novo.
.venv/bin/python painel.py --encerrar || true
# Cria o app, liga ao Claude Desktop e abre o painel. Daqui em diante, tudo é feito na janela do app.
.venv/bin/python painel.py --instalar-app
