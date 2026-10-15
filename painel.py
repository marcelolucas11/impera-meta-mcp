#!/usr/bin/env python3
"""Impera Meta MCP: painel visual do servidor MCP do Meta Ads.

Sobe um servidor só neste computador (127.0.0.1) e abre uma janela para cadastrar
clientes, testar tokens, escolher a conta padrão e exportar relatórios. Usa o mesmo
código do servidor MCP (server.py) e o mesmo cadastro (clientes/<nome>.env).

  painel.py --abrir          abre o painel (sobe o servidor se não estiver no ar)
  painel.py --instalar-app   cria o app em ~/Applications, registra no Claude e abre
  painel.py --servir         só o servidor (uso interno)

Segurança: escuta apenas em 127.0.0.1, exige uma chave aleatória em toda chamada da
API, confere Host e Origin, e nunca devolve um token para a janela.
"""

import argparse
import plistlib
import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import atualizar  # noqa: E402
import configurar  # noqa: E402
import server  # noqa: E402

ESTADO = BASE / ".painel.json"
ROTULO_LAUNCHD = "local.impera.meta-mcp"
PERMISSOES_VALIDAS = set(server.PERMISSOES)
HTML = BASE / "painel.html"
RECURSOS = BASE / "recursos"
NOME_APP = "Impera Meta MCP"
NOMES_ANTIGOS = ("Meta Ads MCP",)  # versões anteriores do app, trocadas por este
OCIOSO_S = 300  # sem janela aberta por este tempo, o servidor do painel se encerra sozinho
INSTANCIA = secrets.token_hex(6)  # muda a cada processo; a janela usa para saber que o painel reiniciou
ABRIR_NO_SISTEMA = True  # os testes desligam, para não abrir Finder/navegador

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = re.compile(r"^[A-Za-z0-9_\-|.~]+$")
_EVENTO = re.compile(r"^[a-z0-9_.]+$")


class Erro(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ───────────────────────── operações ─────────────────────────

def _raiz_relatorios() -> Path:
    d = server._cfg("META_EXPORT_DIR")
    return Path(d).expanduser() if d else server.BASE_DIR / "relatorios"


def _cliente_existente(cid: str) -> dict:
    if not _ID.match(cid or ""):
        raise Erro("Cliente inválido.")
    cs = configurar.clientes()
    if cid not in cs:
        raise Erro("Cliente não encontrado.", 404)
    return cs[cid]


def _relatorios(cid: str) -> list:
    pasta = _raiz_relatorios() / cid
    if not pasta.is_dir():
        return []
    arqs = sorted(pasta.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    return [{"nome": p.name, "kb": round(p.stat().st_size / 1024),
             "quando": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M")} for p in arqs]


def _claude() -> dict:
    caminho = configurar.caminho_config_claude()
    registrado = False
    try:
        cfg = json.loads(caminho.read_text(encoding="utf-8"))
        registrado = (cfg.get("mcpServers") or {}).get(configurar.NOME_SERVIDOR) == configurar.entrada_servidor()
    except (OSError, ValueError, AttributeError):
        pass
    return {"registrado": registrado, "instalado": caminho.parent.is_dir()}


def op_estado() -> dict:
    cs = configurar.clientes()
    raiz = configurar.ler_env(configurar.ENV).get("META_ACCESS_TOKEN", "")
    return {
        "clientes": [{
            "id": cid, "nome": v.get("NOME") or cid,
            "conta_padrao": v.get("META_AD_ACCOUNT_ID", ""),
            "evento_compra": v.get("META_PURCHASE_ACTION") or "auto",
            "pasta": str(_raiz_relatorios() / cid),
            "relatorios": _relatorios(cid),
            "permissoes": server._parse_permissoes(v.get("PERMISSOES")),
            "teto": v.get("ORCAMENTO_MAX_DIA", ""),
        } for cid, v in cs.items()],
        "rotulos_permissoes": server.PERMISSOES,
        "segundo_plano": segundo_plano(),
        "segundo_plano_disponivel": sys.platform == "darwin" and bool(shutil.which("launchctl")),
        "token_antigo": bool(raiz) and raiz != configurar.PLACEHOLDER,
        "claude": _claude(),
        "versao_api": server._api_version(),
        "hoje": date.today().isoformat(),
        "versao": atualizar.versao_local(),
        "instancia": INSTANCIA,
    }


def op_salvar_cliente(dados: dict) -> dict:
    nome = " ".join(str(dados.get("nome", "")).split())[:60]
    cid = str(dados.get("id") or "") or configurar.slug(nome)
    if not _ID.match(cid):
        raise Erro("Dê um nome ao cliente (letras e números).")
    if cid == "padrao":
        raise Erro("'padrao' é reservado. Escolha outro nome.")
    token = str(dados.get("token", "")).strip().strip('"').strip("'")
    existe = cid in configurar.clientes()
    if dados.get("novo") and existe:
        raise Erro(f"Já existe um cliente '{cid}'. Abra-o na lista para trocar o token.")
    if not token and not existe:
        raise Erro("Cole o token do cliente.")
    valores = {}
    if token:
        if not _TOKEN.match(token):
            raise Erro("Esse texto não parece um token da Meta (tem espaços ou símbolos). Copie o token inteiro, sem aspas.")
        valores["META_ACCESS_TOKEN"] = token
    if existe and nome:
        valores["NOME"] = nome
    configurar.salvar_cliente(cid, valores, nome)
    return {"id": cid}


async def _testar(cid: str) -> dict:
    server._usar_cliente(cid)
    async with server._client() as c:
        me = await server._get(c, "me", {"fields": "id,name"})
        perms = None
        try:
            p = await server._get(c, "me/permissions", {"limit": 200})
            perms = sorted(x["permission"] for x in p.get("data", []) if x.get("status") == "granted")
        except server.MetaError:
            pass
        contas = await server._list_accounts(c)
    avisos = []
    if perms is not None and not ({"ads_read", "ads_management"} & set(perms)):
        avisos.append("O token não tem a permissão ads_read. Sem ela não há relatório.")
    escrita = server._parse_permissoes(server._cfg("PERMISSOES"))
    if perms is not None and escrita and "ads_management" not in perms:
        avisos.append("Há permissões de alteração ligadas, mas o token não tem ads_management: as alterações vão falhar "
                      "até gerar um token com essa permissão na BM.")
    if not contas:
        avisos.append("O token não enxerga nenhuma conta de anúncios. Na BM, atribua a conta ao usuário do sistema.")
    return {
        "dono": me.get("name"), "permissoes": perms, "avisos": avisos,
        "pode_escrever": perms is None or "ads_management" in perms,
        "contas": [{
            "id": str(a.get("account_id")), "nome": a.get("name") or str(a.get("account_id")),
            "status": server._ACCOUNT_STATUS.get(a.get("account_status"), a.get("account_status")),
            "moeda": a.get("currency"), "bm": (a.get("business") or {}).get("name"),
        } for a in sorted(contas, key=lambda x: str(x.get("name")))],
    }


async def _resumo(cid: str) -> dict:
    server._usar_cliente(cid)
    d2 = date.today() - timedelta(days=1)
    d1 = d2 - timedelta(days=6)
    async with server._client() as c:
        acc = await server._resolve_account(c, None)
        rows = await server._insights(c, acc["act"], "account", d1, d2, False, [], asyncio.Semaphore(2))
    t = rows[0] if rows else None
    return {
        "conta": acc["nome"], "conta_id": acc["id"], "moeda": acc["moeda"],
        "periodo": f"{d1.strftime('%d/%m')} a {d2.strftime('%d/%m')}",
        "sem_veiculacao": t is None,
        "gasto": t and t["gasto"], "compras": t and t["compras"], "valor": t and t["valor_compras"],
        "roas": t and t["roas"], "cpa": t and t["cpa"],
    }


def _meta(coro) -> dict:
    """Roda uma operação contra a Meta e devolve erro legível, sem token."""
    try:
        return {"ok": True, **asyncio.run(coro)}
    except server.MetaError as e:
        return {"ok": False, "erro": server._scrub(str(e))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Erro inesperado ({type(e).__name__}): {server._scrub(str(e))}"}


def op_config(cid: str, dados: dict) -> dict:
    _cliente_existente(cid)
    valores = {}
    if "conta_padrao" in dados:
        conta = re.sub(r"^act_", "", str(dados["conta_padrao"]).strip())
        if conta and not conta.isdigit():
            raise Erro("ID de conta inválido.")
        valores["META_AD_ACCOUNT_ID"] = conta
    if "evento_compra" in dados:
        ev = str(dados["evento_compra"]).strip() or "auto"
        if not _EVENTO.match(ev):
            raise Erro("Evento de compra inválido. Use 'auto' ou algo como offsite_conversion.custom.123.")
        valores["META_PURCHASE_ACTION"] = ev
    if "nome" in dados:
        nome = " ".join(str(dados["nome"]).split())[:60]
        if nome:
            valores["NOME"] = nome
    if "permissoes" in dados:
        lista = dados["permissoes"] if isinstance(dados["permissoes"], list) else []
        if any(str(x) not in PERMISSOES_VALIDAS for x in lista):
            raise Erro("Permissão desconhecida.")
        valores["PERMISSOES"] = ",".join(x for x in server.PERMISSOES if x in lista)
    if "teto" in dados:
        bruto = str(dados["teto"] or "").strip()
        teto = server._numero(bruto)
        if bruto and (teto is None or teto <= 0):
            raise Erro("Teto de orçamento inválido. Use um valor como 500 ou 1500,00, ou deixe vazio para sem teto.")
        valores["ORCAMENTO_MAX_DIA"] = "" if teto is None else (f"{teto:.2f}".rstrip("0").rstrip("."))
    if valores:
        configurar.salvar_cliente(cid, valores)
    return {"ok": True}


def op_historico(cid: str) -> dict:
    _cliente_existente(cid)
    return {"acoes": server.historico(cid, 30)}


# ───────────────────────── segundo plano (launchd) ─────────────────────────

def _plist_launchd() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{ROTULO_LAUNCHD}.plist"


def segundo_plano() -> bool:
    return configurar.ler_env(configurar.ENV).get("SEGUNDO_PLANO", "") == "1"


def _launchctl(*args: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("launchctl"):
        return False
    return subprocess.run(["launchctl", *args], capture_output=True, timeout=20, check=False).returncode == 0


def _escrever_plist() -> Path:
    destino = _plist_launchd()
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "wb") as f:
        plistlib.dump({
            "Label": ROTULO_LAUNCHD,
            "ProgramArguments": [sys.executable, str(BASE / "painel.py"), "--servir", "--reaproveitar"],
            "EnvironmentVariables": {"IMPERA_LAUNCHD": "1"},
            "WorkingDirectory": str(BASE),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # volta sozinho se cair ou depois de atualizar
            "StandardOutPath": str(BASE / "painel.log"),
            "StandardErrorPath": str(BASE / "painel.log"),
            "ProcessType": "Background",
        }, f)
    return destino


def op_segundo_plano(srv, ativo: bool) -> dict:
    """Ligado: o painel roda pelo launchd (continua com o app fechado e volta ao ligar o Mac).
    Desligado: fechar o app encerra o painel."""
    alvo = f"gui/{os.getuid()}"
    configurar.gravar_env(configurar.ENV, {"SEGUNDO_PLANO": "1" if ativo else "0"})
    if ativo:
        _escrever_plist()

        def assumir():  # o processo do launchd assume a mesma porta e chave; a janela nem percebe
            time.sleep(0.8)
            _launchctl("bootout", f"{alvo}/{ROTULO_LAUNCHD}")  # se houver um serviço antigo carregado
            srv.manter_estado = True
            _launchctl("bootstrap", alvo, str(_plist_launchd()))  # o novo espera esta porta liberar
            srv.shutdown()

        if sys.platform == "darwin" and shutil.which("launchctl") and not os.environ.get("IMPERA_LAUNCHD"):
            threading.Thread(target=assumir, daemon=True).start()
    else:
        try:
            _plist_launchd().unlink()
        except OSError:
            pass
        if os.environ.get("IMPERA_LAUNCHD"):
            def sair_do_launchd():  # um processo comum assume a porta antes de o launchd encerrar este
                time.sleep(0.8)
                log = open(BASE / "painel.log", "a")
                subprocess.Popen([sys.executable, str(BASE / "painel.py"), "--servir", "--reaproveitar"],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                                 cwd=str(BASE), env={k: v for k, v in os.environ.items() if k != "IMPERA_LAUNCHD"})
                srv.manter_estado = True
                _launchctl("bootout", f"{alvo}/{ROTULO_LAUNCHD}")
                srv.shutdown()

            threading.Thread(target=sair_do_launchd, daemon=True).start()
        else:
            _launchctl("bootout", f"{alvo}/{ROTULO_LAUNCHD}")
    return {"ok": True, "segundo_plano": ativo}


def op_remover(cid: str) -> dict:
    _cliente_existente(cid)
    configurar.arquivo_cliente(cid).unlink()
    return {"ok": True}


def op_exportar(cid: str, dados: dict) -> dict:
    _cliente_existente(cid)
    texto = asyncio.run(server.exportar_relatorio(
        cliente=cid, desde=str(dados.get("desde") or "") or None, ate=str(dados.get("ate") or "") or None))
    ok = texto.startswith("Arquivo gerado:")
    return {"ok": ok, "texto": texto, "arquivo": Path(texto.splitlines()[0].split(": ", 1)[1]).name if ok else None,
            "relatorios": _relatorios(cid)}


def op_abrir(dados: dict) -> dict:
    cid = str(dados.get("cliente", ""))
    _cliente_existente(cid)
    pasta = (_raiz_relatorios() / cid).resolve()
    pasta.mkdir(parents=True, exist_ok=True)
    alvo = pasta
    if dados.get("arquivo"):
        alvo = (pasta / str(dados["arquivo"])).resolve()
        if alvo.parent != pasta or not alvo.is_file():
            raise Erro("Arquivo não encontrado.", 404)
    if ABRIR_NO_SISTEMA and sys.platform == "darwin":
        cmd = ["open", str(alvo)] if dados.get("modo") == "abrir" or alvo == pasta else ["open", "-R", str(alvo)]
        subprocess.run(cmd, check=False)
    return {"ok": True, "caminho": str(alvo)}


_busca_cache: dict = {}


def op_buscar_atualizacao(forcar: bool) -> dict:
    agora = time.time()
    if not forcar and _busca_cache.get("quando", 0) > agora - 600:
        return _busca_cache["res"]
    try:
        info = atualizar.buscar()
        res = {"ok": True, **{k: info.get(k) for k in ("atual", "nova", "disponivel", "notas", "publicada", "pagina", "mensagem")}}
    except atualizar.ErroAtualizacao as e:
        res = {"ok": False, "erro": str(e), "atual": atualizar.versao_local()}
    _busca_cache.update(quando=agora, res=res)
    return res


def op_aplicar_atualizacao(srv) -> dict:
    """Baixa e instala a versão nova (só arquivos do programa), refaz o app e reinicia o painel
    na mesma porta e com a mesma chave, para a janela aberta continuar funcionando."""
    try:
        res = atualizar.atualizar()
    except atualizar.ErroAtualizacao as e:
        return {"ok": False, "erro": str(e)}
    if not res.get("atualizado"):
        return {"ok": True, **res}
    _busca_cache.clear()
    # O código novo refaz o app e confere a ligação com o Claude.
    subprocess.run([sys.executable, str(BASE / "painel.py"), "--pos-atualizacao"], cwd=str(BASE),
                   capture_output=True, timeout=120, check=False)

    def reiniciar():
        time.sleep(0.8)  # deixa a resposta chegar na janela
        if os.environ.get("IMPERA_LAUNCHD"):
            srv.manter_estado, srv.codigo_saida = True, 75
            srv.shutdown()
            return
        log = open(BASE / "painel.log", "a")
        subprocess.Popen([sys.executable, str(BASE / "painel.py"), "--servir", "--reaproveitar"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=str(BASE))
        srv.manter_estado = True  # o processo novo regrava o estado; este não pode apagá-lo
        srv.shutdown()

    threading.Thread(target=reiniciar, daemon=True).start()
    return {"ok": True, **res, "reiniciando": True}


def op_registrar_claude() -> dict:
    try:
        status = configurar.registrar_no_claude(configurar.caminho_config_claude())
    except OSError as e:
        status = f"erro: {e}"
    return {"ok": status in ("ok", "igual"), "status": status, "claude": _claude()}


def op_migrar(dados: dict) -> dict:
    nome = " ".join(str(dados.get("nome", "")).split())[:60]
    cid = configurar.slug(nome)
    if not _ID.match(cid) or cid == "padrao":
        raise Erro("Dê um nome ao cliente desse token.")
    if cid in configurar.clientes():
        raise Erro(f"Já existe um cliente '{cid}'.")
    if not configurar.migrar_instalacao_antiga(cid, nome):
        raise Erro("Não há token antigo para mover.")
    return {"id": cid}


# ───────────────────────── servidor HTTP local ─────────────────────────

class Painel(ThreadingHTTPServer):
    daemon_threads = True
    chave = ""
    manter_estado = False  # True quando outro processo assume a mesma porta e chave (atualização)
    codigo_saida = 0  # 75 sob o launchd = "reinicie com o código novo"
    ultimo = 0.0
    ativos = 0
    trava = threading.Lock()


_TIPOS = {".woff2": "font/woff2", ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "MetaAdsPainel"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # nada de log de requisição: o corpo pode ter token
        pass

    def _enviar(self, status: int, corpo: bytes, tipo: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, status: int, obj: dict) -> None:
        self._enviar(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _origem_ok(self) -> bool:
        porta = self.server.server_address[1]
        validos = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
        if self.headers.get("Host", "") not in validos:  # barra DNS rebinding
            return False
        origem = self.headers.get("Origin")
        return origem is None or origem in {f"http://{h}" for h in validos}

    def _tratar(self, metodo: str) -> None:
        srv: Painel = self.server  # type: ignore[assignment]
        with srv.trava:
            srv.ultimo = time.time()
            srv.ativos += 1
        try:
            caminho = self.path.split("?", 1)[0]
            if not self._origem_ok():
                return self._json(403, {"erro": "Origem não permitida."})
            if metodo == "GET" and caminho == "/":
                return self._enviar(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if metodo == "GET" and caminho.startswith("/recursos/"):
                arq = (RECURSOS / caminho[len("/recursos/"):]).resolve()
                if arq.parent != RECURSOS.resolve() or not arq.is_file() or arq.suffix not in _TIPOS:
                    return self._json(404, {"erro": "Não encontrado."})
                return self._enviar(200, arq.read_bytes(), _TIPOS[arq.suffix])
            if not caminho.startswith("/api/"):
                return self._json(404, {"erro": "Não encontrado."})
            if not hmac.compare_digest(self.headers.get("X-Painel-Chave", ""), srv.chave):
                return self._json(401, {"erro": "Sessão do painel inválida. Feche esta janela e abra o app de novo."})
            dados = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                if n > 20000:
                    return self._json(413, {"erro": "Requisição grande demais."})
                try:
                    dados = json.loads(self.rfile.read(n).decode("utf-8"))
                except ValueError:
                    return self._json(400, {"erro": "JSON inválido."})
                if not isinstance(dados, dict):
                    return self._json(400, {"erro": "JSON inválido."})
            self._json(200, self._rota(metodo, caminho[len("/api/"):], dados))
        except Erro as e:
            self._json(e.status, {"erro": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"erro": f"Erro interno ({type(e).__name__}): {server._scrub(str(e))}"})
        finally:
            with srv.trava:
                srv.ativos -= 1
                srv.ultimo = time.time()

    def _rota(self, metodo: str, rota: str, dados: dict) -> dict:
        partes = rota.strip("/").split("/")
        if metodo == "GET" and partes == ["estado"]:
            return op_estado()
        if metodo == "POST" and partes == ["ping"]:
            return {"ok": True}
        if metodo == "POST" and partes == ["clientes"]:
            return op_salvar_cliente(dados)
        if metodo == "POST" and partes == ["abrir"]:
            return op_abrir(dados)
        if metodo == "POST" and partes == ["migrar"]:
            return op_migrar(dados)
        if metodo == "POST" and partes == ["claude", "registrar"]:
            return op_registrar_claude()
        if metodo == "POST" and partes == ["atualizacao", "buscar"]:
            return op_buscar_atualizacao(bool(dados.get("forcar")))
        if metodo == "POST" and partes == ["atualizacao", "aplicar"]:
            return op_aplicar_atualizacao(self.server)
        if metodo == "POST" and partes == ["segundo-plano"]:
            return op_segundo_plano(self.server, bool(dados.get("ativo")))
        if metodo == "POST" and partes == ["encerrar"]:
            if os.environ.get("IMPERA_LAUNCHD"):  # sob o launchd, encerrar = reiniciar com o código instalado
                self.server.manter_estado, self.server.codigo_saida = True, 75
            def encerrar(srv=self.server):
                time.sleep(0.3)  # deixa a resposta chegar antes de fechar
                srv.shutdown()
            threading.Thread(target=encerrar, daemon=True).start()
            return {"ok": True}
        if len(partes) >= 2 and partes[0] == "clientes":
            cid = partes[1]
            if metodo == "DELETE" and len(partes) == 2:
                return op_remover(cid)
            if metodo == "POST" and len(partes) == 3:
                _cliente_existente(cid)
                acao = partes[2]
                if acao == "testar":
                    return _meta(_testar(cid))
                if acao == "resumo":
                    return _meta(_resumo(cid))
                if acao == "config":
                    return op_config(cid, dados)
                if acao == "exportar":
                    return op_exportar(cid, dados)
                if acao == "historico":
                    return op_historico(cid)
        raise Erro("Rota desconhecida.", 404)

    def do_GET(self):
        self._tratar("GET")

    def do_POST(self):
        self._tratar("POST")

    def do_DELETE(self):
        self._tratar("DELETE")


def _gravar_estado(porta: int, chave: str) -> None:
    fd = os.open(str(ESTADO), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"porta": porta, "chave": chave, "pid": os.getpid()}, f)
    os.chmod(ESTADO, 0o600)


def servir(porta: int = 0, ocioso: int = OCIOSO_S, reaproveitar: bool = False) -> None:
    chave = None
    if reaproveitar:  # depois de uma atualização: mesma porta e mesma chave da janela já aberta
        try:
            est = json.loads(ESTADO.read_text())
            porta, chave = int(est["porta"]), str(est["chave"])
        except (OSError, ValueError, KeyError):
            pass
    srv = None
    for _ in range(60):  # a versão anterior pode levar alguns instantes para liberar a porta
        try:
            srv = Painel(("127.0.0.1", porta), Handler)
            break
        except OSError:
            if not reaproveitar or not porta:
                raise
            time.sleep(0.25)
    if srv is None:
        srv = Painel(("127.0.0.1", 0), Handler)
        chave = None
    srv.chave = chave or secrets.token_urlsafe(32)
    srv.ultimo = time.time()
    _gravar_estado(srv.server_address[1], srv.chave)

    def vigia():
        proxima_busca = time.time() + 120
        while True:
            time.sleep(5)
            fundo = segundo_plano() or bool(os.environ.get("IMPERA_LAUNCHD"))
            with srv.trava:
                parado = srv.ativos == 0 and not fundo and time.time() - srv.ultimo > ocioso
            if parado:
                srv.shutdown()
                return
            if fundo and time.time() > proxima_busca:  # em segundo plano, avisa quando sair versão nova
                proxima_busca = time.time() + 6 * 3600
                _avisar_versao_nova()

    threading.Thread(target=vigia, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        try:
            atual = json.loads(ESTADO.read_text())
            if atual.get("pid") == os.getpid() and not srv.manter_estado:
                ESTADO.unlink()
        except (OSError, ValueError):
            pass
    if srv.codigo_saida:
        sys.exit(srv.codigo_saida)


def _avisar_versao_nova() -> None:
    try:
        info = atualizar.buscar()
    except atualizar.ErroAtualizacao:
        return
    marca = BASE / ".aviso-versao"
    if not info.get("disponivel") or (marca.exists() and marca.read_text().strip() == info["nova"]):
        return
    marca.write_text(info["nova"])
    if sys.platform == "darwin":
        texto = f"Versão {info['nova']} disponível. Abra o app e clique em Atualizar agora."
        subprocess.run(["osascript", "-e", f'display notification "{texto}" with title "{NOME_APP}"'],
                       capture_output=True, timeout=10, check=False)


# ───────────────────────── abrir / instalar ─────────────────────────

def _no_ar() -> Optional[dict]:
    try:
        est = json.loads(ESTADO.read_text())
        req = urllib.request.Request(f"http://127.0.0.1:{int(est['porta'])}/api/ping", data=b"{}", method="POST",
                                     headers={"X-Painel-Chave": est["chave"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return est if r.status == 200 else None
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def _versao_no_ar(est: dict) -> Optional[str]:
    """Versão do painel que está rodando (a 4.0 não informava: vira None)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(est['porta'])}/api/estado",
                                     headers={"X-Painel-Chave": est["chave"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8")).get("versao")
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def encerrar_servidor() -> bool:
    """Encerra o painel em execução, de qualquer versão (todas têm /api/encerrar). True se havia um."""
    est = _no_ar()
    if not est:
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(est['porta'])}/api/encerrar", data=b"{}", method="POST",
                                     headers={"X-Painel-Chave": est["chave"], "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except (OSError, ValueError, urllib.error.URLError):
        pass
    for _ in range(40):  # espera o processo antigo sair (sob o launchd, outro assume no lugar)
        time.sleep(0.25)
        agora = _no_ar()
        if not agora or agora.get("pid") != est.get("pid"):
            break
    try:  # versões antigas podem deixar o arquivo de estado para trás
        atual = json.loads(ESTADO.read_text())
        if atual.get("pid") == est.get("pid") and not _no_ar():
            ESTADO.unlink()
    except (OSError, ValueError):
        pass
    return True


def garantir_servidor() -> dict:
    est = _no_ar()
    if est and _versao_no_ar(est) == atualizar.versao_local():
        return est
    if est:
        # O programa foi atualizado no disco, mas o painel em execução ainda é o código antigo:
        # encerra e sobe de novo, senão a janela nova conversa com o servidor velho.
        encerrar_servidor()
        for _ in range(20):  # sob o launchd, o painel novo volta sozinho
            est = _no_ar()
            if est and _versao_no_ar(est) == atualizar.versao_local():
                return est
            if not est:
                break
            time.sleep(0.25)
    log = open(BASE / "painel.log", "a")
    subprocess.Popen([sys.executable, str(BASE / "painel.py"), "--servir"], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, cwd=str(BASE))
    for _ in range(60):
        time.sleep(0.25)
        est = _no_ar()
        if est:
            return est
    raise SystemExit(f"O painel não subiu. Veja {BASE / 'painel.log'}.")


def abrir_janela(url: str) -> None:
    if sys.platform == "darwin":
        # Janela própria, sem barra de endereço, quando há um navegador Chromium instalado.
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
            if any((Path(p) / f"{app}.app").exists() for p in ("/Applications", str(Path.home() / "Applications"))):
                r = subprocess.run(["open", "-na", app, "--args", f"--app={url}", "--window-size=1180,840"], check=False)
                if r.returncode == 0:
                    return
        subprocess.run(["open", url], check=False)
    else:
        webbrowser.open(url)


def _pasta_aplicativos() -> Path:
    # Pasta Aplicativos do Mac quando dá para gravar nela (aparece no Finder e no Launchpad);
    # senão, a pasta Aplicativos do usuário.
    if os.access("/Applications", os.W_OK) and not os.environ.get("PAINEL_FORCAR_APP"):
        return Path("/Applications")
    return Path.home() / "Applications"


def caminho_app() -> Path:
    return _pasta_aplicativos() / f"{NOME_APP}.app"


def _e_nosso(app: Path) -> bool:
    """Só apaga um .app se for mesmo deste programa."""
    try:
        if "painel.py" in (app / "Contents" / "MacOS" / "abrir").read_text(encoding="utf-8"):
            return True
    except OSError:
        pass
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") == "local.impera.meta-mcp"
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False


def _script_do_app() -> str:
    def literal(caminho) -> str:  # dentro de uma string AppleScript
        return str(caminho).replace("\\", "\\\\").replace('"', '\\"')
    modelo = (RECURSOS / "app.applescript").read_text(encoding="utf-8")
    return modelo.replace("__PYTHON__", literal(sys.executable)).replace("__PAINEL__", literal(BASE / "painel.py"))


def _ajustar_bundle(app: Path, executavel: Optional[str], icone: str) -> None:
    info_path = app / "Contents" / "Info.plist"
    try:
        with open(info_path, "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        info = {}
    versao = atualizar.versao_local()
    info.update({
        "CFBundleName": NOME_APP, "CFBundleDisplayName": NOME_APP, "CFBundleIdentifier": "local.impera.meta-mcp",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": versao, "CFBundleVersion": versao,
        "CFBundleIconFile": icone, "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        # a janela carrega o painel em http://127.0.0.1
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    })
    if executavel:
        info["CFBundleExecutable"] = executavel
    info.pop("LSUIElement", None)  # aparece no Dock, com o ponto de "aberto"
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)


def _criar_applet(destino: Path) -> bool:
    """App de verdade, feito pelo osacompile do próprio Mac: ícone no Dock com o ponto de aberto,
    uma janela só (clicar de novo só traz a janela para a frente) e fechar = sair."""
    if sys.platform != "darwin" or not shutil.which("osacompile"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        fonte = Path(tmp) / "app.applescript"
        fonte.write_text(_script_do_app(), encoding="utf-8")
        montado = Path(tmp) / f"{NOME_APP}.app"
        r = subprocess.run(["osacompile", "-s", "-o", str(montado), str(fonte)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not montado.is_dir():
            print(f"osacompile falhou: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
            return False
        recursos = montado / "Contents" / "Resources"
        if (RECURSOS / "icone.icns").exists():
            for nome in ("applet.icns", "icone.icns"):
                shutil.copyfile(RECURSOS / "icone.icns", recursos / nome)
        _ajustar_bundle(montado, None, "applet")
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(montado)], capture_output=True, timeout=120, check=False)
        if destino.exists():
            shutil.rmtree(destino)
        shutil.move(str(montado), str(destino))
    return True


def _criar_lancador(app: Path) -> None:
    """Plano B (sem osacompile): ícone que abre o painel numa janela do navegador."""
    macos, res = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    lancador = macos / "abrir"
    lancador.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{BASE / "painel.py"}" --abrir-navegador >> "{BASE / "painel.log"}" 2>&1\n',
                        encoding="utf-8")
    lancador.chmod(0o755)
    if (RECURSOS / "icone.icns").exists():
        shutil.copyfile(RECURSOS / "icone.icns", res / "icone.icns")
    _ajustar_bundle(app, "abrir", "icone")
    with open(app / "Contents" / "Info.plist", "rb") as f:
        info = plistlib.load(f)
    info["LSUIElement"] = True  # o lançador só abre o navegador e sai
    with open(app / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(info, f)


def criar_app() -> Optional[Path]:
    """Monta o .app neste computador. Por ser criado aqui (e não baixado), o macOS o abre
    sem o aviso de desenvolvedor não identificado."""
    if sys.platform != "darwin" and not os.environ.get("PAINEL_FORCAR_APP"):
        return None
    app = caminho_app()
    # Remove o app com o nome antigo, só se for mesmo o nosso.
    for antigo in NOMES_ANTIGOS:
        for base in (Path("/Applications"), Path.home() / "Applications"):
            velho = base / f"{antigo}.app"
            if velho.exists() and _e_nosso(velho):
                shutil.rmtree(velho, ignore_errors=True)
    app.parent.mkdir(parents=True, exist_ok=True)
    if app.exists() and not _e_nosso(app):
        raise SystemExit(f"Já existe um {app} que não é deste programa; não vou sobrescrever.")
    if not _criar_applet(app):
        if app.exists():
            shutil.rmtree(app)
        _criar_lancador(app)
    os.utime(app, None)  # faz o Finder reler o ícone
    lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", str(app)], capture_output=True, timeout=60, check=False)
    return app


def abrir_app() -> bool:
    """Abre (ou traz para a frente) o app. O macOS garante uma instância só."""
    app = caminho_app()
    if sys.platform == "darwin" and app.exists():
        return subprocess.run(["open", "-a", str(app)], check=False).returncode == 0
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Impera Meta MCP: painel visual")
    ap.add_argument("--abrir", action="store_true")
    ap.add_argument("--servir", action="store_true")
    ap.add_argument("--instalar-app", action="store_true")
    ap.add_argument("--sem-janela", action="store_true")
    ap.add_argument("--porta", type=int, default=0)
    ap.add_argument("--ocioso", type=int, default=OCIOSO_S)
    ap.add_argument("--reaproveitar", action="store_true")
    ap.add_argument("--encerrar", action="store_true")
    ap.add_argument("--pos-atualizacao", action="store_true")
    ap.add_argument("--url", action="store_true", help="garante o painel no ar e imprime o endereço (usado pelo app)")
    ap.add_argument("--abrir-navegador", action="store_true")
    ap.add_argument("--ao-fechar-app", action="store_true")
    ap.add_argument("--motivo", default="")
    a = ap.parse_args()

    if a.url:
        est = garantir_servidor()
        print(f"http://127.0.0.1:{est['porta']}/#k={est['chave']}")
        return
    if a.abrir_navegador:
        if a.motivo:
            print(f"Janela própria indisponível ({a.motivo[:200]}); abrindo no navegador.", file=sys.stderr)
        est = garantir_servidor()
        abrir_janela(f"http://127.0.0.1:{est['porta']}/#k={est['chave']}")
        return
    if a.ao_fechar_app:
        if not segundo_plano():
            encerrar_servidor()
        return

    if a.servir:
        servir(a.porta, a.ocioso, a.reaproveitar)
        return
    if a.pos_atualizacao:
        criar_app()
        op_registrar_claude()
        return
    if a.encerrar:
        print("Painel encerrado." if encerrar_servidor() else "O painel não estava aberto.")
        return
    if a.instalar_app:
        app = criar_app()
        if app:
            print(f"App criado: {app}")
        r = op_registrar_claude()
        print("Claude Desktop: " + {"ok": "servidor 'meta-ads' registrado. Feche o Claude (Cmd+Q) e abra de novo.",
                                     "igual": "servidor 'meta-ads' já estava registrado."}.get(r["status"], "não registrado (" + r["status"] + "); use o botão no painel."))
    if not (a.sem_janela or os.environ.get("PAINEL_SEM_JANELA")) and abrir_app():
        if a.instalar_app:
            print(f"App aberto. Da próxima vez, abra o \"{NOME_APP}\" pelo Dock, pelo Spotlight, pelo Launchpad ou pela pasta Aplicativos.")
        return
    est = garantir_servidor()
    url = f"http://127.0.0.1:{est['porta']}/#k={est['chave']}"
    if a.sem_janela or os.environ.get("PAINEL_SEM_JANELA"):
        print(f"Painel no ar na porta {est['porta']}.")
    else:
        abrir_janela(url)
        if a.instalar_app:
            print(f"Painel aberto. Da próxima vez, abra o app \"{NOME_APP}\" pelo Spotlight, pelo Launchpad ou pela pasta Aplicativos.")


if __name__ == "__main__":
    main()
