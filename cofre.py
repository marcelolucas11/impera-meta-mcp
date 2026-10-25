#!/usr/bin/env python3
"""Cofre de tokens no Chaveiro (Keychain) do macOS.

Em vez de deixar o token em texto no arquivo clientes/<nome>.env, o arquivo guarda só o aviso
`keychain:impera-meta-mcp` e o token fica no Chaveiro do Mac, protegido pela senha do usuário.
O server.py lê do Chaveiro sozinho (pelo programa `security` do próprio macOS) e nunca grava o token de volta.

Uso, no Terminal:
  .venv/bin/python cofre.py --status     mostra quais clientes estão no cofre e quais ainda estão em texto
  .venv/bin/python cofre.py --migrar     move os tokens para o Chaveiro (confere a leitura antes de trocar)
  .venv/bin/python cofre.py --desfazer   devolve os tokens para os arquivos (se quiser voltar ao modo antigo)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

SERVICO = "impera-meta-mcp"
MARCA = f"keychain:{SERVICO}"
CHAVES = ("META_ACCESS_TOKEN", "META_APP_SECRET")
_cache: dict = {}  # conta -> (instante, valor): evita chamar o `security` a cada requisição
_CACHE_SEGUNDOS = 600


def disponivel() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _executar(args: list, entrada: Optional[str] = None) -> tuple:
    """Roda o `security`. Os comandos com segredo vão pela entrada padrão (modo -i), nunca na linha de comando,
    para o token não aparecer na lista de processos. Substituível nos testes."""
    r = subprocess.run(["security"] + args, input=entrada, capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout, r.stderr


def _conta(cliente: str, chave: str) -> str:
    return f"{cliente}:{chave}"


def _aspas(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def guardar(cliente: str, chave: str, valor: str) -> None:
    rc, _o, e = _executar(["-i"], f"add-generic-password -U -a {_aspas(_conta(cliente, chave))} -s {_aspas(SERVICO)} -w {_aspas(valor)}\n")
    if rc != 0:
        raise RuntimeError("o Chaveiro recusou guardar o segredo")
    _cache.pop(_conta(cliente, chave), None)


def ler(cliente: str, chave: str) -> Optional[str]:
    conta = _conta(cliente, chave)
    t = _cache.get(conta)
    if t and time.time() - t[0] < _CACHE_SEGUNDOS:
        return t[1]
    rc, out, _e = _executar(["find-generic-password", "-a", conta, "-s", SERVICO, "-w"])
    if rc != 0:
        return None
    valor = out.rstrip("\n")
    _cache[conta] = (time.time(), valor)
    return valor or None


def apagar(cliente: str, chave: str) -> None:
    _executar(["delete-generic-password", "-a", _conta(cliente, chave), "-s", SERVICO])
    _cache.pop(_conta(cliente, chave), None)


def resolver(cliente: str, valores: dict) -> dict:
    """Troca o aviso `keychain:...` pelo segredo de verdade (só em memória). Se o Chaveiro não responder, deixa o aviso:
    quem usa o token (server._token) explica o problema."""
    out = dict(valores)
    for k in CHAVES:
        if str(out.get(k, "")).startswith("keychain:"):
            s = ler(cliente, k)
            if s:
                out[k] = s
    return out


# ───────────────────────── linha de comando ─────────────────────────

def _linhas(arq: Path) -> list:
    return arq.read_text(encoding="utf-8").splitlines()


def _definir(arq: Path, chave: str, valor: str) -> None:
    linhas, achou = [], False
    for l in _linhas(arq):
        if l.split("=", 1)[0].strip() == chave:
            linhas.append(f"{chave}={valor}")
            achou = True
        else:
            linhas.append(l)
    if not achou:
        linhas.append(f"{chave}={valor}")
    tmp = arq.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(tmp, arq)


def _valores(arq: Path) -> dict:
    out = {}
    for l in _linhas(arq):
        if "=" in l and not l.strip().startswith("#"):
            k, v = l.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def migrar(pasta_clientes: Path, placeholder: str = "COLE_O_TOKEN_NOVO_AQUI") -> list:
    rel = []
    for arq in sorted(Path(pasta_clientes).glob("*.env")):
        v = _valores(arq)
        for chave in CHAVES:
            s = v.get(chave, "")
            if not s or s == placeholder or s.startswith("keychain:"):
                continue
            guardar(arq.stem, chave, s)
            if ler(arq.stem, chave) != s:  # só troca o arquivo se o Chaveiro devolveu exatamente o mesmo valor
                rel.append(f"{arq.stem}.{chave}: NÃO migrado (o Chaveiro não devolveu o mesmo valor); arquivo mantido.")
                continue
            _definir(arq, chave, MARCA)
            rel.append(f"{arq.stem}.{chave}: movido para o Chaveiro.")
    return rel


def desfazer(pasta_clientes: Path) -> list:
    rel = []
    for arq in sorted(Path(pasta_clientes).glob("*.env")):
        v = _valores(arq)
        for chave in CHAVES:
            if str(v.get(chave, "")).startswith("keychain:"):
                s = ler(arq.stem, chave)
                if not s:
                    rel.append(f"{arq.stem}.{chave}: não consegui ler do Chaveiro; arquivo mantido.")
                    continue
                _definir(arq, chave, s)
                rel.append(f"{arq.stem}.{chave}: devolvido ao arquivo.")
    return rel


def status(pasta_clientes: Path) -> list:
    rel = []
    for arq in sorted(Path(pasta_clientes).glob("*.env")):
        v = _valores(arq)
        for chave in CHAVES:
            s = v.get(chave, "")
            if not s:
                continue
            if s.startswith("keychain:"):
                rel.append(f"{arq.stem}.{chave}: no Chaveiro ({'ok' if ler(arq.stem, chave) else 'NÃO CONSEGUI LER'})")
            elif s.startswith("COLE_"):
                rel.append(f"{arq.stem}.{chave}: ainda não preenchido")
            else:
                rel.append(f"{arq.stem}.{chave}: em TEXTO no arquivo")
    return rel


def main(argv: list) -> int:
    pasta = Path(__file__).resolve().parent / "clientes"
    if not disponivel():
        print("O Chaveiro só existe no macOS (programa 'security'). Nada foi feito.")
        return 1
    if "--migrar" in argv:
        out = migrar(pasta)
    elif "--desfazer" in argv:
        out = desfazer(pasta)
    elif "--status" in argv:
        out = status(pasta)
    else:
        print(__doc__)
        return 0
    print("\n".join(out) or "Nada a fazer.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
