#!/usr/bin/env python3
"""Configuração guiada: um token por cliente, guardado em clientes/<nome>.env.

  python configurar.py                 adiciona clientes ou troca tokens
  python configurar.py --listar        mostra os clientes configurados (sem tokens)
  python configurar.py --remover NOME  apaga o token de um cliente

Os tokens são digitados aqui, no Terminal. Nunca vão para o chat.
"""

import asyncio
import getpass
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
CLIENTES = BASE / "clientes"
PLACEHOLDER = "COLE_O_TOKEN_NOVO_AQUI"
NOME_SERVIDOR = "meta-ads"
_CHAVES_CLIENTE = ["NOME", "META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_BUSINESS_ID",
                   "META_PURCHASE_ACTION", "META_APP_SECRET", "CONTAS_LIBERADAS"]


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()


def ler_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def gravar_env(path: Path, valores: dict) -> None:
    """Atualiza chaves preservando o resto do arquivo. Permissão 600 (só o seu usuário lê)."""
    linhas = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pendentes = dict(valores)
    saida = []
    for raw in linhas:
        s = raw.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in pendentes:
                saida.append(f"{k}={pendentes.pop(k)}")
                continue
        saida.append(raw)
    saida += [f"{k}={v}" for k, v in pendentes.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(saida) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def arquivo_cliente(nome: str) -> Path:
    return CLIENTES / f"{nome}.env"


def clientes() -> dict:
    out = {}
    if CLIENTES.is_dir():
        for f in sorted(CLIENTES.glob("*.env")):
            v = ler_env(f)
            if v.get("META_ACCESS_TOKEN") and v["META_ACCESS_TOKEN"] != PLACEHOLDER:
                out[f.stem] = v
    return out


def salvar_cliente(nome: str, valores: dict, exibicao: str = "") -> Path:
    CLIENTES.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CLIENTES, 0o700)
    except OSError:
        pass
    caminho = arquivo_cliente(nome)
    if not caminho.exists():
        valores = {"NOME": exibicao or nome, "META_AD_ACCOUNT_ID": "", "META_PURCHASE_ACTION": "auto", **valores}
    gravar_env(caminho, valores)
    return caminho


def migrar_instalacao_antiga(nome: str, exibicao: str = "") -> bool:
    """Versão anterior guardava um único token no .env da raiz. Move para clientes/<nome>.env."""
    raiz = ler_env(ENV)
    token = raiz.get("META_ACCESS_TOKEN", "")
    if not token or token == PLACEHOLDER:
        return False
    salvar_cliente(nome, {k: raiz[k] for k in _CHAVES_CLIENTE if raiz.get(k)}, exibicao)
    gravar_env(ENV, {k: "" for k in _CHAVES_CLIENTE if k in raiz})
    return True


def caminho_config_claude() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def entrada_servidor() -> dict:
    # sys.executable é o Python do .venv; caminho absoluto porque o Claude não herda o PATH do terminal.
    return {"command": sys.executable, "args": [str(BASE / "server.py")]}


def registrar_no_claude(cfg_path: Path) -> str:
    """Acrescenta o servidor ao claude_desktop_config.json sem mexer no resto. Faz backup antes."""
    cfg = {}
    if cfg_path.exists() and cfg_path.read_text(encoding="utf-8").strip():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError:
            return "invalido"
        if not isinstance(cfg, dict):
            return "invalido"
    servidores = cfg.setdefault("mcpServers", {})
    if not isinstance(servidores, dict):
        return "invalido"
    novo = entrada_servidor()
    if servidores.get(NOME_SERVIDOR) == novo:
        return "igual"
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.with_name(cfg_path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    servidores[NOME_SERVIDOR] = novo
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "ok"


def perguntar(texto: str, padrao: str = "") -> str:
    try:
        r = input(texto).strip()
    except EOFError:
        r = ""
    return r or padrao


def mostrar_clientes() -> None:
    cs = clientes()
    if not cs:
        print("Nenhum cliente configurado ainda.")
        return
    print("Clientes configurados:")
    for n, v in cs.items():
        print(f"  - {n}   conta padrão: {v.get('META_AD_ACCOUNT_ID') or 'não definida'}")


def configurar_cliente(nome: str, server, exibicao: str = "") -> None:
    existe = nome in clientes()
    if existe and perguntar(f"'{nome}' já tem token. Trocar? [s/N] ").lower() not in ("s", "sim"):
        print("Mantendo o token atual.")
    else:
        print(f"Cole o token do cliente '{nome}' e aperte Enter.")
        print("(nada aparece na tela enquanto você cola; é assim mesmo)")
        try:
            token = getpass.getpass("Token: ").strip().strip('"').strip("'")
        except EOFError:
            token = ""
        if not token:
            print("Nenhum token informado; nada foi alterado.")
            return
        caminho = salvar_cliente(nome, {"META_ACCESS_TOKEN": token}, exibicao)
        del token
        print(f"Token gravado em {caminho} (só o seu usuário consegue ler).")

    print("Testando a conexão com a Meta...")
    resultado = asyncio.run(server.verificar_conexao(nome))
    print(resultado)
    if "Conexão OK" not in resultado:
        print("O token ficou salvo. Corrija o problema acima e rode o configurador de novo para este cliente.")
        return

    async def _contas():
        server._usar_cliente(nome)
        async with server._client() as c:
            return await server._list_accounts(c)
    try:
        contas = sorted(asyncio.run(_contas()), key=lambda a: str(a.get("name")))
    except Exception as e:
        print(f"(não consegui listar as contas: {type(e).__name__})")
        return
    if not contas:
        print("Este token não enxerga nenhuma conta de anúncios. Atribua a conta ao usuário do sistema na BM.")
        return
    print("\nContas que este token enxerga:")
    for i, a in enumerate(contas, 1):
        print(f"  {i}. {a.get('name')}  ({a.get('account_id')})")
    atual = ler_env(arquivo_cliente(nome)).get("META_AD_ACCOUNT_ID", "")
    dica = f" [Enter mantém {atual}]" if atual else " [Enter para pular]"
    escolha = perguntar(f"Número da conta padrão de '{nome}'{dica}: ")
    if escolha.isdigit() and 1 <= int(escolha) <= len(contas):
        conta = contas[int(escolha) - 1]
        salvar_cliente(nome, {"META_AD_ACCOUNT_ID": str(conta.get("account_id"))})
        print(f"Conta padrão de '{nome}': {conta.get('name')} ({conta.get('account_id')}).")


def main(argv: Optional[list] = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if "--listar" in argv:
        mostrar_clientes()
        return
    if "--remover" in argv:
        i = argv.index("--remover")
        nome = slug(argv[i + 1]) if i + 1 < len(argv) else ""
        caminho = arquivo_cliente(nome)
        if nome and caminho.exists():
            caminho.unlink()
            print(f"Cliente '{nome}' removido (token apagado deste computador). "
                  "Para invalidar o token de vez, revogue-o também na BM.")
        else:
            print(f"Cliente '{nome}' não encontrado.")
            mostrar_clientes()
        return

    print("\n=== Impera Meta MCP: clientes ===\n")
    sys.path.insert(0, str(BASE))
    import server

    raiz = ler_env(ENV).get("META_ACCESS_TOKEN", "")
    if raiz and raiz != PLACEHOLDER:
        print("Encontrei o token da instalação anterior (um cliente só).")
        digitado = perguntar("Nome do cliente desse token (ex.: primos): ")
        nome = slug(digitado)
        if nome and nome != "padrao" and migrar_instalacao_antiga(nome, digitado):
            print(f"Movido para o cliente '{nome}'.\n")
    del raiz

    mostrar_clientes()
    while True:
        digitado = perguntar("\nNome do cliente para adicionar ou atualizar (Enter para terminar): ")
        nome = slug(digitado)
        if not nome:
            break
        if nome == "padrao":
            print("'padrao' é reservado; escolha outro nome.")
            continue
        configurar_cliente(nome, server, digitado)

    print()
    mostrar_clientes()
    cfg_path = caminho_config_claude()
    print(f"\nRegistrando no Claude Desktop ({cfg_path})...")
    try:
        status = registrar_no_claude(cfg_path)
    except OSError as e:
        status = f"erro: {e}"
    if status == "ok":
        print("Servidor 'meta-ads' registrado.")
        print("\nÚltimo passo: feche o Claude por completo (Cmd+Q) e abra de novo.")
        print('Depois, num chat novo, peça: "lista os clientes do Meta Ads".')
    elif status == "igual":
        print("Servidor 'meta-ads' já estava registrado. Clientes novos já valem, sem reiniciar o Claude.")
    else:
        print("Não alterei o arquivo de configuração do Claude (" + status + ").")
        print("No Claude: Configurações > Desenvolvedor > Editar configuração, e inclua dentro de \"mcpServers\":\n")
        print(json.dumps({NOME_SERVIDOR: entrada_servidor()}, indent=2, ensure_ascii=False))
    print()


if __name__ == "__main__":
    main()
