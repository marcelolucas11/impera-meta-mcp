#!/bin/bash
# Instala o Impera Meta MCP: ambiente Python, token (digitado aqui, fora do chat) e registro no Claude.
set -e
cd "$(dirname "$0")"
ORIGEM="$(pwd -P)"
DESTINO="$HOME/meta-ads-mcp"

# Pastas protegidas do macOS (Downloads, Documentos, Mesa) impedem o Claude de iniciar o servidor.
case "$ORIGEM" in
  "$HOME/Downloads"*|"$HOME/Documents"*|"$HOME/Desktop"*)
    echo "Copiando para $DESTINO (o Claude não consegue rodar o servidor a partir de $ORIGEM)..."
    mkdir -p "$DESTINO"
    cp -R "$ORIGEM/." "$DESTINO/"
    cd "$DESTINO"
    ;;
esac

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
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

# Cria o app, liga ao Claude Desktop e abre o painel. Daqui em diante, tudo é feito na janela do app.
.venv/bin/python painel.py --instalar-app
