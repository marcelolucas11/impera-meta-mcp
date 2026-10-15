#!/usr/bin/env python3
"""Atualização do Impera Meta MCP pelas releases do GitHub.

Só substitui arquivos do programa. Nunca toca no que é do usuário: clientes e
tokens (clientes/), ajustes (.env), relatórios, o ambiente Python (.venv) e o
estado do app. Antes de copiar, guarda a versão atual em .versoes-anteriores/.

  atualizar.py --verificar   mostra se há versão nova
  atualizar.py --aplicar     baixa, confere e instala a versão nova
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional

import httpx

BASE = Path(__file__).resolve().parent
REPO = "marcelolucas11/impera-meta-mcp"
PACOTE = "impera-meta-mcp.zip"
PAGINA_RELEASES = f"https://github.com/{REPO}/releases"

# Itens da raiz que pertencem ao usuário e nunca são escritos por uma atualização.
PRESERVAR = {".env", "clientes", "relatorios", ".venv", ".painel.json", "painel.log",
             ".versoes-anteriores", ".git"}
LIMITE_BYTES = 60 * 1024 * 1024


class ErroAtualizacao(Exception):
    pass


def versao_local(base: Optional[Path] = None) -> str:
    try:
        return ((base or BASE) / "VERSAO").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


def _tupla(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def _url_releases() -> str:
    return os.environ.get("IMPERA_URL_RELEASES") or f"https://api.github.com/repos/{REPO}/releases/latest"


def _cliente_http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True,
                        headers={"User-Agent": "impera-meta-mcp", "Accept": "application/vnd.github+json"})


def buscar() -> dict:
    """Consulta a última release publicada."""
    atual = versao_local()
    try:
        with _cliente_http() as c:
            r = c.get(_url_releases())
    except httpx.HTTPError as e:
        raise ErroAtualizacao(f"Não consegui falar com o GitHub ({type(e).__name__}). Confira a internet e tente de novo.")
    if r.status_code == 404:
        return {"atual": atual, "disponivel": False, "nova": None, "mensagem": "Nenhuma versão publicada ainda."}
    if r.status_code == 403:
        raise ErroAtualizacao("O GitHub limitou as consultas deste computador por agora. Tente de novo em uma hora.")
    if r.status_code != 200:
        raise ErroAtualizacao(f"O GitHub respondeu HTTP {r.status_code} ao buscar a versão nova.")
    d = r.json()
    nova = str(d.get("tag_name") or "").lstrip("vV")
    assets = {a.get("name"): a.get("browser_download_url") for a in d.get("assets") or []}
    return {
        "atual": atual,
        "nova": nova,
        "disponivel": bool(nova) and _tupla(nova) > _tupla(atual) and PACOTE in assets,
        "notas": (d.get("body") or "").strip(),
        "publicada": (d.get("published_at") or "")[:10],
        "url": assets.get(PACOTE),
        "url_sha256": assets.get(PACOTE + ".sha256"),
        "pagina": d.get("html_url") or PAGINA_RELEASES,
    }


def _baixar(c: httpx.Client, url: str, destino: Path) -> None:
    with c.stream("GET", url) as r:
        if r.status_code != 200:
            raise ErroAtualizacao(f"Falha ao baixar a versão nova (HTTP {r.status_code}).")
        total = 0
        with open(destino, "wb") as f:
            for parte in r.iter_bytes():
                total += len(parte)
                if total > LIMITE_BYTES:
                    raise ErroAtualizacao("O arquivo da versão nova é maior do que o esperado. Atualização cancelada.")
                f.write(parte)


def baixar_e_conferir(info: dict, pasta: Path) -> Path:
    """Baixa o pacote e confere o SHA-256 publicado junto. Sem checksum, não instala."""
    if not info.get("url") or not info.get("url_sha256"):
        raise ErroAtualizacao("A versão publicada não traz o pacote e a assinatura (checksum) esperados.")
    zip_path = pasta / PACOTE
    with _cliente_http() as c:
        _baixar(c, info["url"], zip_path)
        r = c.get(info["url_sha256"])
        if r.status_code != 200:
            raise ErroAtualizacao("Não consegui baixar o checksum da versão nova.")
        esperado = (r.text.split() or [""])[0].lower()
    obtido = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", esperado) or obtido != esperado:
        raise ErroAtualizacao("O arquivo baixado não confere com o checksum publicado. Nada foi alterado.")
    return zip_path


def extrair(zip_path: Path, pasta: Path) -> Path:
    """Extrai com segurança e devolve a pasta do programa (a que tem VERSAO e painel.py)."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ErroAtualizacao("O pacote baixado está corrompido. Nada foi alterado.")
    with zf:
        for info in zf.infolist():
            p = PurePosixPath(info.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in info.filename:
                raise ErroAtualizacao("O pacote tem caminhos inválidos. Atualização recusada; nada foi alterado.")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ErroAtualizacao("O pacote tem links simbólicos. Atualização recusada; nada foi alterado.")
        destino = pasta / "extraido"
        zf.extractall(destino)
    candidatos = [p.parent for p in destino.rglob("VERSAO") if (p.parent / "painel.py").is_file()]
    if len(candidatos) != 1:
        raise ErroAtualizacao("O pacote não tem a estrutura esperada. Nada foi alterado.")
    return candidatos[0]


def _arquivos_do_pacote(raiz: Path) -> list:
    out = []
    for p in sorted(raiz.rglob("*")):
        rel = p.relative_to(raiz)
        if rel.parts[0] in PRESERVAR or "__pycache__" in rel.parts:
            continue
        if p.is_file():
            out.append(rel)
    return out


def aplicar(raiz: Path, base: Optional[Path] = None) -> dict:
    """Copia os arquivos do programa por cima da instalação, guardando antes a versão atual."""
    base = base or BASE
    antiga, nova = versao_local(base), versao_local(raiz)
    arquivos = _arquivos_do_pacote(raiz)
    if not arquivos:
        raise ErroAtualizacao("O pacote está vazio. Nada foi alterado.")
    backup = base / ".versoes-anteriores" / f"{antiga}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    for rel in arquivos:
        atual = base / rel
        if atual.is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(atual, backup / rel)
    for rel in arquivos:
        alvo = base / rel
        alvo.parent.mkdir(parents=True, exist_ok=True)
        tmp = alvo.with_name(alvo.name + ".novo")
        shutil.copy2(raiz / rel, tmp)
        os.replace(tmp, alvo)  # troca atômica arquivo a arquivo
    return {"de": antiga, "para": nova, "arquivos": len(arquivos), "backup": str(backup)}


def instalar_dependencias(base: Optional[Path] = None) -> None:
    if os.environ.get("IMPERA_PULAR_DEPENDENCIAS"):
        return
    base = base or BASE
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        "-r", str(base / "requirements.txt")], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise ErroAtualizacao("Os arquivos foram atualizados, mas a instalação de dependências falhou: "
                              + (r.stderr or r.stdout)[-400:])


def atualizar() -> dict:
    info = buscar()
    if not info["disponivel"]:
        return {"atualizado": False, "atual": info["atual"], "mensagem": "Você já está na versão mais nova."}
    pasta = Path(tempfile.mkdtemp(prefix="impera-atualizacao-"))
    try:
        raiz = extrair(baixar_e_conferir(info, pasta), pasta)
        if versao_local(raiz) != info["nova"]:
            raise ErroAtualizacao("A versão dentro do pacote não confere com a publicada. Nada foi alterado.")
        res = aplicar(raiz)
        instalar_dependencias()
        return {"atualizado": True, **res}
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def main() -> None:
    try:
        if "--aplicar" in sys.argv:
            print(json.dumps(atualizar(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(buscar(), ensure_ascii=False, indent=2))
    except ErroAtualizacao as e:
        print(f"ERRO: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
