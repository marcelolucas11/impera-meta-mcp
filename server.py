#!/usr/bin/env python3
"""Meta Ads MCP: servidor local do Impera Meta MCP para o Claude Desktop.

Vários clientes, um token por cliente: cada um fica em clientes/<nome>.env ao lado
deste script. O token vai apenas no cabeçalho Authorization das chamadas à Graph
API: nunca aparece em URL, log ou resposta de ferramenta. Uma chamada usa somente
o token do cliente pedido; não existe token "global" que sirva de reserva.

Leitura sempre. Escrita (ativar/pausar, orçamento, renomear, criar, duplicar, públicos de lista) só
quando o cliente tem a permissão ligada no app, só na conta padrão do cliente,
com tudo criado PAUSADO, respeitando o teto de orçamento diário e com registro
de cada ação em historico/<cliente>.jsonl. Nada é apagado.
"""

import asyncio
import contextlib
import contextvars
import csv
import hashlib
import html
import hmac
import io
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, get_args

import httpx
from pydantic import Field

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"  # só ajustes gerais (versão da API, pasta de saída)
CLIENTES_DIR = BASE_DIR / "clientes"  # um arquivo <nome>.env por cliente
HISTORICO_DIR = BASE_DIR / "historico"  # um <nome>.jsonl por cliente com cada alteração feita pelo Claude
GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v26.0"
PLACEHOLDER = "COLE_O_TOKEN_NOVO_AQUI"

# Ajustáveis nos testes.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_BACKOFF = (4, 12, 30)  # segundos entre tentativas em limite de chamadas / erro transitório
JANELA_DIAS = 7  # tamanho do bloco de datas nas consultas dia a dia
PAGINA = 500
MAX_SIMULTANEAS = 4

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("meta-ads-mcp")


# ───────────────────────── configuração ─────────────────────────

# Chaves que pertencem a um cliente. Nunca são lidas de outro lugar que não o
# arquivo do cliente ativo: é isso que impede usar o token de um na conta de outro.
_POR_CLIENTE = {"META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_BUSINESS_ID",
                "META_PURCHASE_ACTION", "META_APP_SECRET", "NOME", "PERMISSOES", "ORCAMENTO_MAX_DIA",
                "EXIGIR_PERIODO", "CONTAS_LIBERADAS"}
_ATIVO: contextvars.ContextVar = contextvars.ContextVar("cliente_ativo", default=None)
_CONTA_USADA: contextvars.ContextVar = contextvars.ContextVar("conta_usada", default="")  # conta da alteração em curso, para o histórico


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except (FileNotFoundError, NotADirectoryError):
        pass
    return out


class MetaError(Exception):
    def __init__(self, message: str, code: Any = None, too_much: bool = False):
        super().__init__(message)
        self.code = code
        self.too_much = too_much


def _como_configurar() -> str:
    return f"No Terminal, rode:  {BASE_DIR / '.venv' / 'bin' / 'python'} {BASE_DIR / 'configurar.py'}"


def _clientes() -> dict:
    """Clientes configurados, relidos a cada chamada (adicionar ou trocar token não exige
    reiniciar o Claude). Compatibilidade: um token no .env da raiz, de uma instalação
    antiga, aparece como cliente 'padrao'."""
    out: dict = {}
    if CLIENTES_DIR.is_dir():
        for f in sorted(CLIENTES_DIR.glob("*.env")):
            v = _read_env(f)
            if v.get("META_ACCESS_TOKEN") and v["META_ACCESS_TOKEN"] != PLACEHOLDER:
                out[f.stem] = v
    raiz = _read_env(ENV_FILE)
    legado = os.environ.get("META_ACCESS_TOKEN") or raiz.get("META_ACCESS_TOKEN", "")
    if legado and legado != PLACEHOLDER and "padrao" not in out:
        v = {k: os.environ.get(k) or raiz.get(k, "") for k in _POR_CLIENTE}
        v["META_ACCESS_TOKEN"] = legado
        out["padrao"] = v
    return out


def _usar_cliente(cliente: Optional[str]) -> str:
    """Seleciona o cliente desta chamada. Com mais de um configurado, o nome é obrigatório:
    nunca se escolhe um cliente por omissão."""
    cs = _clientes()
    if not cs:
        raise MetaError("Nenhum cliente configurado. " + _como_configurar()
                        + " (o token é digitado lá, nunca no chat).")
    alvo = (cliente or "").strip()
    if not alvo:
        if len(cs) > 1:
            raise MetaError("Há mais de um cliente configurado (" + ", ".join(cs)
                            + "). Informe qual no parâmetro 'cliente'.")
        nome = next(iter(cs))
    else:
        chave = _slug(alvo)
        if chave in cs:
            nome = chave
        else:
            hits = [n for n, v in cs.items() if chave in n or chave in _slug(v.get("NOME", ""))]
            if len(hits) != 1:
                raise MetaError(f"Cliente '{alvo}' " + ("não está configurado" if not hits else "bate com mais de um")
                                + ". Configurados: " + ", ".join(cs) + ".")
            nome = hits[0]
    _ATIVO.set(dict(cs[nome], _id=nome))
    _CONTA_USADA.set("")
    return nome


def _cliente_id() -> str:
    return (_ATIVO.get() or {}).get("_id", "")


def _cfg(name: str, default: str = "") -> str:
    if name in _POR_CLIENTE:
        v = (_ATIVO.get() or {}).get(name, "")
    else:
        v = os.environ.get(name) or _read_env(ENV_FILE).get(name, "")
    return (v or "").strip() or default


def _token() -> str:
    t = _cfg("META_ACCESS_TOKEN")
    if not t or t == PLACEHOLDER:
        raise MetaError("Nenhum cliente selecionado para esta chamada. " + _como_configurar())
    return t


_SEGREDOS_PAGINA: set = set()  # tokens de página obtidos em tempo de execução; nunca aparecem em respostas


def _bearer(client: httpx.AsyncClient) -> str:
    """Token que este cliente HTTP realmente usa (o do cliente ou, em leads, o da página)."""
    h = client.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else _token()


def _scrub(text: str) -> str:
    for t in _SEGREDOS_PAGINA:
        text = text.replace(t, "***")
    for v in _clientes().values():
        t = v.get("META_ACCESS_TOKEN")
        if t:
            text = text.replace(t, "***")
    return text


def _api_version() -> str:
    v = _cfg("META_API_VERSION", DEFAULT_API_VERSION)
    return v if v.startswith("v") else f"v{v}"


def _export_dir() -> Path:
    """Uma subpasta por cliente, para os arquivos de um nunca se misturarem com os de outro."""
    d = _cfg("META_EXPORT_DIR")
    p = (Path(d).expanduser() if d else BASE_DIR / "relatorios") / (_cliente_id() or "padrao")
    p.mkdir(parents=True, exist_ok=True)
    return p


# ───────────────────────── HTTP ─────────────────────────

_RATE_CODES = {4, 17, 32, 613}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=httpx.Timeout(120.0, connect=20.0),
        transport=_TRANSPORT,
    )


def _friendly(code: Any, msg: str, status: int) -> str:
    msg = _scrub(msg)
    if code == 190:
        return (f"A Meta recusou o token do cliente '{_cliente_id()}' (inválido, expirado ou revogado). "
                f"Gere um token novo no usuário do sistema da BM e troque-o. {_como_configurar()} Detalhe: {msg}")
    if code in (10, 200, 294) or (isinstance(code, int) and 200 <= code <= 299):
        return ("O token não tem permissão para isso. Ele precisa do escopo ads_read e o usuário do "
                f"sistema precisa estar atribuído à conta de anúncios. Detalhe: {msg}")
    if code == 100:
        return f"A Meta rejeitou um parâmetro da consulta. Detalhe: {msg}"
    if code in _RATE_CODES or (isinstance(code, int) and 80000 <= code <= 80014):
        return ("Limite de chamadas da Meta atingido para esta conta. Espere alguns minutos e tente "
                f"de novo, ou reduza o período. Detalhe: {msg}")
    return f"Erro da Meta (código {code}, HTTP {status}): {msg}"


async def _get(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> dict:
    url = f"{GRAPH_HOST}/{_api_version()}/{path.lstrip('/')}"
    params = dict(params or {})
    secret = _cfg("META_APP_SECRET")
    if secret:
        params["appsecret_proof"] = hmac.new(secret.encode(), _bearer(client).encode(), hashlib.sha256).hexdigest()

    for attempt in range(len(_BACKOFF) + 1):
        last = attempt == len(_BACKOFF)
        try:
            r = await client.get(url, params=params)
        except httpx.TimeoutException:
            raise MetaError("A Meta demorou demais para responder esta consulta.", too_much=True)
        except httpx.HTTPError as e:
            if not last:
                await asyncio.sleep(_BACKOFF[attempt])
                continue
            raise MetaError(f"Falha de rede ao falar com a Meta ({type(e).__name__}).")

        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code == 200 and isinstance(data, dict) and "error" not in data:
            return data

        err = data.get("error", {}) if isinstance(data, dict) else {}
        code = err.get("code")
        msg = str(err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}")
        if "reduce the amount of data" in msg.lower() or (code == 1 and r.status_code >= 500):
            raise MetaError(_friendly(code, msg, r.status_code), code=code, too_much=True)
        retryable = (
            code in _RATE_CODES
            or (isinstance(code, int) and 80000 <= code <= 80014)
            or err.get("is_transient") is True
            or code == 2
            or (r.status_code >= 500 and not err)
        )
        if retryable and not last:
            await asyncio.sleep(_BACKOFF[attempt])
            continue
        raise MetaError(_friendly(code, msg, r.status_code), code=code)
    raise MetaError("Falha inesperada ao consultar a Meta.")


async def _get_all(client: httpx.AsyncClient, path: str, params: dict, sem: Optional[asyncio.Semaphore] = None) -> list:
    """Percorre a paginação por cursor (nunca segue a URL 'next', que traz o token)."""
    out: list = []
    p = dict(params)
    while True:
        if sem:
            async with sem:
                data = await _get(client, path, p)
        else:
            data = await _get(client, path, p)
        out.extend(data.get("data") or [])
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not after:
            return out
        p["after"] = after


# ───────────────────────── utilidades ─────────────────────────

_ZERO_DECIMAL = {"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"}
_ACCOUNT_STATUS = {
    1: "ATIVA", 2: "DESATIVADA", 3: "PAGAMENTO PENDENTE", 7: "EM ANÁLISE DE RISCO",
    8: "LIQUIDAÇÃO PENDENTE", 9: "PERÍODO DE CARÊNCIA", 100: "FECHAMENTO PENDENTE", 101: "FECHADA",
}


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _minor(v: Any, currency: str) -> Optional[float]:
    """Orçamentos e lances vêm na menor unidade da moeda (centavos em BRL)."""
    if v in (None, "", "0", 0):
        return None
    return _f(v) / (1 if currency in _ZERO_DECIMAL else 100)


def _r(v: Optional[float], n: int = 2) -> Any:
    return None if v is None else round(v, n)


def _parse_date(s: str, campo: str) -> date:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise MetaError(f"Data inválida em '{campo}': {s!r}. Use o formato AAAA-MM-DD.")


def _exige_periodo(valor: Optional[str] = None) -> bool:
    v = _cfg("EXIGIR_PERIODO") if valor is None else valor
    return (v or "").strip().lower() in ("1", "sim", "true")


def _checar_periodo(desde: Optional[str], ate: Optional[str]) -> None:
    """Com a chave 'Exigir período' ligada, nenhuma análise roda com um período que o usuário não escolheu."""
    if _exige_periodo() and not (desde and ate):
        raise MetaError(
            f"O cliente '{_cliente_id()}' exige período nas análises (chave 'Exigir período' no app). "
            "Se o usuário já disse o período nesta conversa (ex.: 'últimos 15 dias', 'de 1 a 10'), converta para "
            "desde e ate no formato AAAA-MM-DD e chame de novo. Se não disse, pergunte qual é a data inicial e a "
            "final antes de consultar. Nada foi consultado.")


def _periodo(desde: Optional[str], ate: Optional[str]) -> tuple:
    d2 = _parse_date(ate, "ate") if ate else date.today() - timedelta(days=1)
    d1 = _parse_date(desde, "desde") if desde else d2 - timedelta(days=6)
    if d1 > d2:
        raise MetaError("'desde' não pode ser depois de 'ate'.")
    return d1, d2


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "conta"


def _act(conta_id: str) -> str:
    digits = re.sub(r"\D", "", conta_id)
    if not digits:
        raise MetaError(f"ID de conta inválido: {conta_id!r}.")
    return f"act_{digits}"


async def _list_accounts(client: httpx.AsyncClient) -> list:
    fields = "account_id,name,account_status,currency,timezone_name,business{id,name}"
    seen: dict = {}
    errors = []
    edges = ["me/adaccounts", "me/assigned_ad_accounts"]
    bm = re.sub(r"\D", "", _cfg("META_BUSINESS_ID"))
    if bm:
        edges += [f"{bm}/owned_ad_accounts", f"{bm}/client_ad_accounts"]
    for edge in edges:
        if edge == "me/assigned_ad_accounts" and seen:
            continue
        try:
            for a in await _get_all(client, edge, {"fields": fields, "limit": 200}):
                seen.setdefault(str(a.get("account_id")), a)
        except MetaError as e:
            if e.code == 190:
                raise
            errors.append(str(e))
    if not seen and errors:
        raise MetaError(errors[0])
    return list(seen.values())


async def _resolve_account(client: httpx.AsyncClient, conta: Optional[str]) -> dict:
    """Aceita ID (com ou sem 'act_'), um trecho do nome da conta, ou nada (usa a conta padrão)."""
    alvo = (conta or "").strip() or _cfg("META_AD_ACCOUNT_ID")
    if not alvo:
        raise MetaError(f"Nenhuma conta informada e o cliente '{_cliente_id()}' não tem conta padrão. "
                        "Use listar_contas e passe o ID ou o nome.")
    if re.fullmatch(r"(act_)?\d{6,}", alvo):
        info = await _get(client, _act(alvo), {"fields": "account_id,name,currency,timezone_name,account_status"})
    else:
        contas = await _list_accounts(client)
        hits = [a for a in contas if alvo.lower() in str(a.get("name", "")).lower()]
        if not hits:
            raise MetaError(f"Nenhuma conta com '{alvo}' no nome. Contas visíveis para este token: "
                            + "; ".join(f"{a.get('name')} ({a.get('account_id')})" for a in contas))
        if len(hits) > 1:
            raise MetaError(f"'{alvo}' bate com mais de uma conta, informe o ID: "
                            + "; ".join(f"{a.get('name')} ({a.get('account_id')})" for a in hits))
        info = hits[0]
    return {
        "id": str(info.get("account_id")),
        "act": _act(str(info.get("account_id"))),
        "nome": info.get("name") or str(info.get("account_id")),
        "moeda": info.get("currency") or "BRL",
        "fuso": info.get("timezone_name") or "",
    }


# ───────────────────────── insights ─────────────────────────

NIVEIS = {"conta": "account", "campanha": "campaign", "conjunto": "adset", "anuncio": "ad"}
QUEBRAS = {
    "nenhuma": [],
    "idade_genero": ["age", "gender"],
    "idade": ["age"],
    "genero": ["gender"],
    "plataforma": ["publisher_platform"],
    "posicionamento": ["publisher_platform", "platform_position"],
    "dispositivo": ["impression_device"],
    "regiao": ["region"],
    "hora": ["hourly_stats_aggregated_by_advertiser_time_zone"],
}
_BREAKDOWN_KEY = {
    "age": "idade", "gender": "genero", "publisher_platform": "plataforma",
    "platform_position": "posicao", "impression_device": "dispositivo", "region": "regiao",
    "hourly_stats_aggregated_by_advertiser_time_zone": "hora",
}
_STATUS = {
    "campaign": ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES"],
    "adset": ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES", "CAMPAIGN_PAUSED"],
    "ad": ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES", "CAMPAIGN_PAUSED",
           "ADSET_PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED", "PENDING_BILLING_INFO"],
}
_PURCHASE = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
_CART = ["omni_add_to_cart", "add_to_cart", "offsite_conversion.fb_pixel_add_to_cart"]
_CHECKOUT = ["omni_initiated_checkout", "initiate_checkout", "offsite_conversion.fb_pixel_initiate_checkout"]
_LPV = ["landing_page_view", "omni_landing_page_view"]


def _purchase_types() -> list:
    custom = _cfg("META_PURCHASE_ACTION")
    return [custom] if custom and custom.lower() != "auto" else _PURCHASE


def _pick(items: Any, candidates: list) -> float:
    """Pega o primeiro tipo de ação presente. Nunca soma tipos: omni_purchase já
    contém purchase e o evento do pixel, somar contaria a mesma compra 2 ou 3 vezes."""
    if not items:
        return 0.0
    by = {i.get("action_type"): i.get("value") for i in items}
    for c in candidates:
        if c in by:
            return _f(by[c])
    return 0.0


def _derive(r: dict) -> dict:
    g, imp, cl, comp, val = r["gasto"], r["impressoes"], r["cliques_link"], r["compras"], r["valor_compras"]
    r["ctr_link"] = cl / imp * 100 if imp else None
    r["cpm"] = g / imp * 1000 if imp else None
    r["cpc_link"] = g / cl if cl else None
    r["cpa"] = g / comp if comp else None
    r["roas"] = val / g if g else None
    r["ticket"] = val / comp if comp else None
    return r


def _norm(row: dict, por_dia: bool) -> dict:
    r = {
        "campanha": row.get("campaign_name"), "campanha_id": row.get("campaign_id"),
        "conjunto": row.get("adset_name"), "conjunto_id": row.get("adset_id"),
        "anuncio": row.get("ad_name"), "anuncio_id": row.get("ad_id"),
        "objetivo": row.get("objective"),
        "dia": row.get("date_start") if por_dia else None,
        "inicio": row.get("date_start"), "fim": row.get("date_stop"),
        "gasto": _f(row.get("spend")), "impressoes": _f(row.get("impressions")),
        "alcance": _f(row.get("reach")) if row.get("reach") is not None else None,
        "frequencia": _f(row.get("frequency")) if row.get("frequency") is not None else None,
        "cliques": _f(row.get("clicks")), "cliques_link": _f(row.get("inline_link_clicks")),
        "lpv": _pick(row.get("actions"), _LPV),
        "carrinho": _pick(row.get("actions"), _CART),
        "checkout": _pick(row.get("actions"), _CHECKOUT),
        "compras": _pick(row.get("actions"), _purchase_types()),
        "valor_compras": _pick(row.get("action_values"), _purchase_types()),
    }
    for api_key, key in _BREAKDOWN_KEY.items():
        if api_key in row:
            r[key] = row[api_key]
    return _derive(r)


_SOMAVEIS = ["gasto", "impressoes", "cliques", "cliques_link", "lpv", "carrinho", "checkout", "compras", "valor_compras"]


def _soma(rows: list) -> dict:
    """Soma só o que é somável. Alcance e frequência são deduplicados e ficam de fora."""
    t = {k: sum(r.get(k) or 0 for r in rows) for k in _SOMAVEIS}
    t["alcance"] = None
    t["frequencia"] = None
    return _derive(t)


_VIDEO = {  # coluna -> campo da API (cada um é uma lista de ações do tipo video_view)
    "video_plays": "video_play_actions",
    "video_p25": "video_p25_watched_actions",
    "video_p50": "video_p50_watched_actions",
    "video_p75": "video_p75_watched_actions",
    "video_p100": "video_p100_watched_actions",
    "thruplays": "video_thruplay_watched_actions",
}


def _vsum(items: Any) -> float:
    """Campos de vídeo vêm como lista de ações (em geral só video_view): soma os valores."""
    if not items:
        return 0.0
    return sum(_f(i.get("value")) for i in items)


def _insight_fields(level: str, com_alcance: bool, com_video: bool = False) -> str:
    f = ["account_id"]
    if level in ("campaign", "adset", "ad"):
        f += ["campaign_id", "campaign_name", "objective"]
    if level in ("adset", "ad"):
        f += ["adset_id", "adset_name"]
    if level == "ad":
        f += ["ad_id", "ad_name"]
    f += ["spend", "impressions", "clicks", "inline_link_clicks", "actions", "action_values"]
    if com_alcance:
        f += ["reach", "frequency"]
    if com_video:
        f += list(_VIDEO.values())
    return ",".join(f)


async def _insights(client: httpx.AsyncClient, act: str, level: str, d1: date, d2: date, por_dia: bool,
                    breakdowns: list, sem: asyncio.Semaphore, campanha_ids: Optional[list] = None,
                    avisos: Optional[list] = None, com_video: bool = False) -> list:
    com_alcance = "hourly_stats_aggregated_by_advertiser_time_zone" not in breakdowns
    base = {
        "level": level,
        "fields": _insight_fields(level, com_alcance, com_video),
        "time_increment": "1" if por_dia else "all_days",
        "use_unified_attribution_setting": "true",
        "limit": PAGINA,
    }
    if breakdowns:
        base["breakdowns"] = ",".join(breakdowns)

    def filtros(com_status: bool) -> list:
        f = []
        if com_status and level in _STATUS:
            # Sem isto a Meta omite anúncios arquivados/excluídos e a soma das linhas
            # fica menor que o total da conta.
            f.append({"field": f"{level}.effective_status", "operator": "IN", "value": _STATUS[level]})
        if campanha_ids:
            f.append({"field": "campaign.id", "operator": "IN", "value": [str(c) for c in campanha_ids]})
        return f

    async def fetch(a: date, b: date, com_status: bool) -> list:
        params = dict(base, time_range=json.dumps({"since": a.isoformat(), "until": b.isoformat()}))
        fl = filtros(com_status)
        if fl:
            params["filtering"] = json.dumps(fl)
        try:
            return await _get_all(client, f"{act}/insights", params, sem)
        except MetaError as e:
            if e.too_much and por_dia and b > a:
                mid = a + (b - a) // 2
                x, y = await asyncio.gather(fetch(a, mid, com_status), fetch(mid + timedelta(days=1), b, com_status))
                return x + y
            raise

    async def run(com_status: bool) -> list:
        if not por_dia:
            return await fetch(d1, d2, com_status)
        janelas, a = [], d1
        while a <= d2:
            b = min(a + timedelta(days=JANELA_DIAS - 1), d2)
            janelas.append((a, b))
            a = b + timedelta(days=1)
        partes = await asyncio.gather(*(fetch(a, b, com_status) for a, b in janelas))
        return [row for parte in partes for row in parte]

    try:
        raw = await run(True)
    except MetaError as e:
        if e.code != 100 or level not in _STATUS:
            raise
        raw = await run(False)
        if avisos is not None:
            avisos.append(f"A Meta recusou o filtro de status no nível {level}; itens arquivados ou "
                          "excluídos podem estar fora das linhas (o total da conta continua completo).")
    rows = [_norm(r, por_dia) for r in raw]
    if com_video:
        for r, row in zip(rows, raw):
            for col, campo in _VIDEO.items():
                r[col] = _vsum(row.get(campo))
    return rows


# ───────────────────────── estrutura ─────────────────────────

def _targeting_resumo(t: Optional[dict]) -> dict:
    t = t or {}
    geo = t.get("geo_locations") or {}
    genders = {1: "homens", 2: "mulheres"}
    return {
        "idade": f"{t.get('age_min', '')}-{t.get('age_max', '')}".strip("-") or None,
        "genero": ", ".join(genders.get(g, str(g)) for g in t.get("genders", [])) or "todos",
        "paises": ", ".join(geo.get("countries", [])) or None,
        "regioes": ", ".join(r.get("name", "") for r in geo.get("regions", [])) or None,
        "cidades": len(geo.get("cities", [])) or None,
        "publicos": ", ".join(a.get("name", a.get("id", "")) for a in t.get("custom_audiences", [])) or None,
        "publicos_excluidos": ", ".join(a.get("name", a.get("id", "")) for a in t.get("excluded_custom_audiences", [])) or None,
        "advantage_publico": (t.get("targeting_automation") or {}).get("advantage_audience"),
    }


async def _buscar_objetos(client: httpx.AsyncClient, edge: str, params: dict, ids: Optional[list],
                          sem: asyncio.Semaphore) -> list:
    """Sem ids: lista o edge inteiro. Com ids: busca só esses (filtro id IN, em lotes); se a Meta
    recusar o filtro, lista tudo e filtra aqui."""
    if ids is None:
        return await _get_all(client, edge, params, sem)
    if not ids:
        return []
    try:
        partes = await asyncio.gather(*(
            _get_all(client, edge, dict(params, filtering=json.dumps(
                [{"field": "id", "operator": "IN", "value": ids[i:i + 100]}])), sem)
            for i in range(0, len(ids), 100)))
        return [x for p in partes for x in p]
    except MetaError as e:
        if e.code != 100:
            raise
        alvo = set(ids)
        return [x for x in await _get_all(client, edge, params, sem) if x.get("id") in alvo]


async def _estrutura(client: httpx.AsyncClient, conta: dict, niveis: set, somente_ativos: bool,
                     sem: asyncio.Semaphore, ids: Optional[dict] = None) -> dict:
    moeda = conta["moeda"]
    extra = {"effective_status": json.dumps(["ACTIVE"])} if somente_ativos and not ids else {}
    ids = ids or {}
    out: dict = {}

    async def campanhas():
        f = ("id,name,objective,status,effective_status,bid_strategy,daily_budget,lifetime_budget,"
             "spend_cap,created_time,start_time,stop_time")
        rows = await _buscar_objetos(client, f"{conta['act']}/campaigns", dict(fields=f, limit=200, **extra), ids.get("campanhas"), sem)
        out["campanhas"] = [{
            "campanha": c.get("name"), "campanha_id": c.get("id"), "objetivo": c.get("objective"),
            "status": c.get("effective_status"), "estrategia_lance": c.get("bid_strategy"),
            "orcamento_diario": _minor(c.get("daily_budget"), moeda),
            "orcamento_total": _minor(c.get("lifetime_budget"), moeda),
            "limite_gasto": _minor(c.get("spend_cap"), moeda),
            "criada_em": (c.get("created_time") or "")[:10] or None,
        } for c in rows]

    async def conjuntos():
        f = ("id,name,campaign_id,status,effective_status,bid_strategy,bid_amount,bid_constraints,"
             "daily_budget,lifetime_budget,optimization_goal,billing_event,created_time,targeting")
        rows = await _buscar_objetos(client, f"{conta['act']}/adsets", dict(fields=f, limit=100, **extra), ids.get("conjuntos"), sem)
        res = []
        for s in rows:
            item = {
                "conjunto": s.get("name"), "conjunto_id": s.get("id"), "campanha_id": s.get("campaign_id"),
                "status": s.get("effective_status"), "estrategia_lance": s.get("bid_strategy"),
                "limite_cpa_ou_lance": _minor(s.get("bid_amount"), moeda),
                "roas_minimo": (_f((s.get("bid_constraints") or {}).get("roas_average_floor")) / 10000
                                if (s.get("bid_constraints") or {}).get("roas_average_floor") else None),
                "orcamento_diario": _minor(s.get("daily_budget"), moeda),
                "orcamento_total": _minor(s.get("lifetime_budget"), moeda),
                "otimizacao": s.get("optimization_goal"),
                "criado_em": (s.get("created_time") or "")[:10] or None,
            }
            item.update(_targeting_resumo(s.get("targeting")))
            res.append(item)
        out["conjuntos"] = res

    async def anuncios():
        f = "id,name,adset_id,campaign_id,status,effective_status,created_time,creative{id,name,object_type,video_id}"
        rows = await _buscar_objetos(client, f"{conta['act']}/ads", dict(fields=f, limit=200, **extra), ids.get("anuncios"), sem)
        out["anuncios"] = [{
            "anuncio": a.get("name"), "anuncio_id": a.get("id"), "conjunto_id": a.get("adset_id"),
            "campanha_id": a.get("campaign_id"), "status": a.get("effective_status"),
            "criativo_id": (a.get("creative") or {}).get("id"),
            "tipo_criativo": (a.get("creative") or {}).get("object_type"),
            "video_id": (a.get("creative") or {}).get("video_id"),
            "criado_em": (a.get("created_time") or "")[:10] or None,
        } for a in rows]

    jobs = []
    if "campanhas" in niveis:
        jobs.append(campanhas())
    if "conjuntos" in niveis:
        jobs.append(conjuntos())
    if "anuncios" in niveis:
        jobs.append(anuncios())
    await asyncio.gather(*jobs)
    return out


# ───────────────────────── saída ─────────────────────────

def _clean(d: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items() if v not in (None, "")}


def _csv(rows: list, cols: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else _cell(c, r[c]) for c in cols])
    return buf.getvalue()


def _resumo_txt(t: dict, moeda: str) -> str:
    def m(v):
        return "n/d" if v is None else f"{moeda} {v:,.2f}"
    partes = [
        f"gasto {m(t['gasto'])}", f"compras {t['compras']:,.0f}", f"valor de conversão {m(t['valor_compras'])}",
        f"ROAS {t['roas']:.2f}" if t["roas"] is not None else "ROAS n/d",
        f"CPA {m(t['cpa'])}", f"ticket {m(t['ticket'])}", f"impressões {t['impressoes']:,.0f}",
    ]
    if t.get("alcance") is not None:
        partes.append(f"alcance {t['alcance']:,.0f}")
    if t.get("frequencia") is not None:
        partes.append(f"frequência {t['frequencia']:.2f}")
    return " | ".join(partes)


def _colunas_xlsx(level: str, por_dia: bool, breakdowns: list, moeda: str) -> list:
    """Cabeçalhos no molde do export em português do Gerenciador de Anúncios."""
    c = []
    if level in ("campaign", "adset", "ad"):
        c += [("Nome da campanha", "campanha"), ("Identificação da campanha", "campanha_id")]
    if level in ("adset", "ad"):
        c += [("Nome do conjunto de anúncios", "conjunto"), ("Identificação do conjunto de anúncios", "conjunto_id")]
    if level == "ad":
        c += [("Nome do anúncio", "anuncio"), ("Identificação do anúncio", "anuncio_id")]
    if level != "account":
        c.append(("Objetivo", "objetivo"))
    if por_dia:
        c.append(("Dia", "dia"))
    nomes = {"idade": "Idade", "genero": "Gênero", "plataforma": "Plataforma", "posicao": "Posicionamento",
             "dispositivo": "Dispositivo de impressão", "regiao": "Região", "hora": "Hora do dia"}
    for b in breakdowns:
        c.append((nomes[_BREAKDOWN_KEY[b]], _BREAKDOWN_KEY[b]))
    c += [
        ("Início dos relatórios", "inicio"), ("Término dos relatórios", "fim"),
        (f"Valor usado ({moeda})", "gasto"), ("Impressões", "impressoes"), ("Alcance", "alcance"),
        ("Frequência", "frequencia"), ("Cliques (todos)", "cliques"), ("Cliques no link", "cliques_link"),
        ("CTR (taxa de cliques no link)", "ctr_link"),
        (f"CPM (custo por 1.000 impressões) ({moeda})", "cpm"),
        (f"CPC (custo por clique no link) ({moeda})", "cpc_link"),
        ("Visualizações da página de destino", "lpv"), ("Adições ao carrinho", "carrinho"),
        ("Finalizações de compra iniciadas", "checkout"), ("Compras", "compras"),
        ("Valor de conversão da compra", "valor_compras"), (f"Custo por compra ({moeda})", "cpa"),
        ("ROAS (retorno do investimento em publicidade) das compras", "roas"),
    ]
    return c


_IDS = {"campanha_id", "conjunto_id", "anuncio_id", "criativo_id"}
_CONTAGENS = {"impressoes", "alcance", "cliques", "cliques_link", "lpv", "carrinho", "checkout", "compras",
              "video_plays", "video_p25", "video_p50", "video_p75", "video_p100", "thruplays"}


def _cell(key: str, v: Any) -> Any:
    if v is None:
        return None
    if key in _IDS:
        return str(v)  # IDs têm 15 a 18 dígitos; como número o Excel arredonda os últimos
    if key in _CONTAGENS and isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, float):
        return round(v, 4 if key in ("frequencia", "ctr_link", "roas") else 2)
    return v


def _sheet(wb, titulo: str, cols: list, rows: list, linha_resumo: Optional[dict] = None) -> None:
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(titulo)
    ws.freeze_panes = "A2"
    for i, (h, _) in enumerate(cols):
        ws.column_dimensions[get_column_letter(i + 1)].width = 44 if h.startswith("Nome") else 22 if "Identificação" in h else 16
    header = []
    for h, _ in cols:
        cell = WriteOnlyCell(ws, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
        header.append(cell)
    ws.append(header)
    if linha_resumo is not None:
        # Linha de resumo da conta, com nomes em branco, igual ao export do Gerenciador.
        ws.append([_cell(k, linha_resumo.get(k)) for _, k in cols])
    for r in rows:
        ws.append([_cell(k, r.get(k)) for _, k in cols])


# ───────────────────────── servidor MCP ─────────────────────────

mcp = _Server(
    "meta-ads",
    instructions=(
        "Marketing API da Meta para vários clientes, cada um com o próprio token. Leitura sempre "
        "disponível. Ferramentas de alteração (alterar_status, alterar_orcamento, renomear, duplicar, "
        "criar_campanha, criar_conjunto, criar_anuncio) só funcionam para clientes com a permissão "
        "ligada no app Impera Meta MCP, só na conta padrão do cliente, e tudo é criado PAUSADO. "
        "Antes de qualquer alteração, diga ao usuário exatamente o que vai mudar (cliente, objeto, "
        "valores antes e depois) e só prossiga com a confirmação dele. Use listar_clientes para ver quem está "
        "configurado e passe 'cliente' em toda chamada quando houver mais de um. Nunca misture dados "
        "de clientes diferentes numa mesma análise ou painel, e diga sempre de qual cliente é cada "
        "número. Para o relatório completo de rifa/sorteio (Tabelas A, B e C do prompt v4) use "
        "exportar_relatorio; para perguntas pontuais use consultar_insights. Alcance e frequência são "
        "deduplicados: nunca some essas colunas entre linhas. Nunca peça nem exiba tokens no chat."
    ),
)
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_CLIENTE = Annotated[Optional[str], Field(
    description="Nome do cliente configurado (veja listar_clientes). Obrigatório quando há mais de um.")]


_CONTA = Annotated[Optional[str], Field(
    description="ID numérico de outra conta de anúncios liberada para este cliente no app (veja listar_clientes, "
                "coluna contas_liberadas). Vazio: usa a conta padrão.")]


def _tool_error(e: Exception) -> str:
    if isinstance(e, MetaError):
        return f"ERRO: {_scrub(str(e))}"
    log.exception("erro inesperado")
    return f"ERRO inesperado ({type(e).__name__}): {_scrub(str(e))}"


def _liberadas_de(v: dict) -> list:
    out: list = []
    for x in [v.get("META_AD_ACCOUNT_ID", "")] + re.split(r"[,;\s]+", v.get("CONTAS_LIBERADAS", "") or ""):
        d = _so_digitos(x)
        if d and d not in out:
            out.append(d)
    return out


@mcp.tool(annotations=_RO)
async def listar_clientes() -> str:
    """Clientes configurados neste computador: nome, conta de anúncios padrão, evento de compra e
    pasta dos relatórios. Nunca mostra tokens."""
    try:
        cs = _clientes()
        if not cs:
            return "Nenhum cliente configurado. " + _como_configurar()
        base = _cfg("META_EXPORT_DIR")
        raiz = Path(base).expanduser() if base else BASE_DIR / "relatorios"
        rows = [{
            "cliente": n, "nome": v.get("NOME") or n, "conta_padrao": v.get("META_AD_ACCOUNT_ID") or "",
            "contas_liberadas": ",".join(_liberadas_de(v)),
            "evento_compra": v.get("META_PURCHASE_ACTION") or "auto", "pasta_relatorios": str(raiz / n),
            "permissoes": "ler" + "".join("+" + x for x in _parse_permissoes(v.get("PERMISSOES"))),
            "teto_orcamento_dia": v.get("ORCAMENTO_MAX_DIA") or "",
            "exige_periodo": "sim" if _exige_periodo(v.get("EXIGIR_PERIODO")) else "não",
        } for n, v in cs.items()]
        return f"{len(rows)} cliente(s):\n" + _csv(rows, ["cliente", "nome", "conta_padrao", "contas_liberadas", "evento_compra", "permissoes",
                                                         "teto_orcamento_dia", "exige_periodo", "pasta_relatorios"])
    except Exception as e:
        return _tool_error(e)


async def _verificar_um() -> str:
    nome = _cliente_id()
    async with _client() as c:
        me = await _get(c, "me", {"fields": "id,name"})
        perms = None
        try:
            p = await _get(c, "me/permissions", {"limit": 200})
            perms = sorted(x["permission"] for x in p.get("data", []) if x.get("status") == "granted")
        except MetaError:
            pass
        contas = await _list_accounts(c)
    linhas = [
        f"[{nome}] Conexão OK. Token de: {me.get('name')} (ID {me.get('id')}).",
        f"Versão da API: {_api_version()}.",
        f"Contas de anúncios visíveis: {len(contas)}.",
    ]
    if perms is not None:
        linhas.append("Permissões: " + (", ".join(perms) or "nenhuma"))
        if not ({"ads_read", "ads_management"} & set(perms)):
            linhas.append("ATENÇÃO: falta ads_read. Sem ela não há relatório.")
        escrita = _parse_permissoes(_cfg("PERMISSOES"))
        if escrita and "ads_management" not in perms:
            linhas.append("ATENÇÃO: o cliente tem permissões de alteração ligadas, mas o token não tem "
                          "ads_management. Alterações vão falhar até gerar um token com essa permissão.")
    linhas.append("Permissões no app: ler" + "".join(f", {PERMISSOES[x]}" for x in _parse_permissoes(_cfg("PERMISSOES"))) + ".")
    linhas.append(f"Conta padrão: {_cfg('META_AD_ACCOUNT_ID') or 'não definida'}.")
    extras = [x for x in _contas_liberadas() if x != _so_digitos(_cfg("META_AD_ACCOUNT_ID"))]
    if extras:
        linhas.append("Outras contas liberadas para alterar (use o parâmetro 'conta'): " + ", ".join(extras) + ".")
    if _exige_periodo():
        linhas.append("Exigir período: ligado. Consultas e relatórios só rodam com data inicial e final informadas.")
    acao = _cfg("META_PURCHASE_ACTION")
    linhas.append("Evento de compra: " + (acao if acao and acao.lower() != "auto"
                                          else "automático (omni_purchase > purchase > pixel)") + ".")
    return "\n".join(linhas)


@mcp.tool(annotations=_RO)
async def verificar_conexao(cliente: _CLIENTE = None) -> str:
    """Testa o token de um cliente (ou de todos, se nenhum for informado): a quem pertence, as
    permissões e quantas contas de anúncios enxerga. Use primeiro quando algo falhar."""
    try:
        if (cliente or "").strip():
            nomes = [_usar_cliente(cliente)]
        else:
            nomes = list(_clientes())
            if not nomes:
                _usar_cliente(None)
        partes = []
        for n in nomes:
            try:
                _usar_cliente(n)
                partes.append(await _verificar_um())
            except Exception as e:
                partes.append(f"[{n}] " + _tool_error(e))
        return "\n\n".join(partes)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_contas(cliente: _CLIENTE = None) -> str:
    """Lista as contas de anúncios que o token do cliente enxerga: ID, nome, status, moeda, fuso e BM."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            contas = await _list_accounts(c)
        if not contas:
            return ("Nenhuma conta de anúncios visível. Na BM, atribua a conta ao usuário do sistema "
                    "(Configurações do negócio > Usuários do sistema > Atribuir ativos).")
        rows = [{
            "id": a.get("account_id"), "nome": a.get("name"),
            "status": _ACCOUNT_STATUS.get(a.get("account_status"), a.get("account_status")),
            "moeda": a.get("currency"), "fuso": a.get("timezone_name"),
            "bm": (a.get("business") or {}).get("name"), "bm_id": (a.get("business") or {}).get("id"),
        } for a in sorted(contas, key=lambda x: str(x.get("name")))]
        return f"Cliente: {nome_cliente} | {len(rows)} conta(s):\n" + _csv(rows, ["id", "nome", "status", "moeda", "fuso", "bm", "bm_id"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def estrutura_conta(
    cliente: _CLIENTE = None,
    conta: Annotated[Optional[str], Field(description="ID da conta (com ou sem act_) ou trecho do nome. Vazio usa a conta padrão.")] = None,
    nivel: Annotated[Literal["campanhas", "conjuntos", "anuncios", "tudo"], Field(description="O que listar.")] = "conjuntos",
    somente_ativos: Annotated[bool, Field(description="True lista só o que está ativo agora. Ignorado quando há período.")] = True,
    max_linhas: Annotated[int, Field(description="Teto de linhas por nível na resposta.", ge=1, le=2000)] = 300,
    desde: Annotated[Optional[str], Field(description="Com período: lista só o que teve veiculação (gasto ou impressões) "
                                                      "entre desde e ate, ativo ou pausado, ordenado por gasto. AAAA-MM-DD.")] = None,
    ate: Annotated[Optional[str], Field(description="Data final do período, AAAA-MM-DD. Vazio com 'desde': ontem.")] = None,
) -> str:
    """Estrutura real da conta: orçamento diário, estratégia de lance, limite de CPA (bid_amount),
    ROAS mínimo, objetivo, status e público de cada campanha/conjunto. No nível anúncios, também o
    tipo do criativo (tipo_criativo: VIDEO, SHARE, PHOTO...) e o video_id. Valores já na moeda da conta.
    Para ANÁLISE, passe desde/ate: vem só o que veiculou no período (inclusive o que foi pausado
    depois), com gasto_periodo, e a resposta fica muito menor. Sem período, lista pelo status de agora
    (use para achar IDs antes de alterar). Serve para conferir os limites de CPA contra o nome da campanha."""
    try:
        nome_cliente = _usar_cliente(cliente)
        niveis = {"campanhas", "conjuntos", "anuncios"} if nivel == "tudo" else {nivel}
        com_periodo = bool(desde or ate)
        sem = asyncio.Semaphore(MAX_SIMULTANEAS)
        avisos: list = []
        idcol = {"campanhas": "campanha_id", "conjuntos": "conjunto_id", "anuncios": "anuncio_id"}
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            if com_periodo:
                d1, d2 = _periodo(desde, ate)
                level = "ad" if "anuncios" in niveis else ("adset" if "conjuntos" in niveis else "campaign")
                linhas = await _insights(c, acc["act"], level, d1, d2, False, [], sem, avisos=avisos)
                gasto: dict = {k: {} for k in idcol}
                for r in linhas:
                    if not ((r.get("impressoes") or 0) > 0 or (r.get("gasto") or 0) > 0):
                        continue
                    for k, col in idcol.items():
                        if r.get(col):
                            gasto[k][r[col]] = gasto[k].get(r[col], 0) + (r.get("gasto") or 0)
                est = await _estrutura(c, acc, niveis, False, sem, {k: list(gasto[k]) for k in niveis})
            else:
                est = await _estrutura(c, acc, niveis, somente_ativos, sem)
        if com_periodo:
            filtro = (f"Só o que veiculou de {d1} a {d2} (ativos e pausados), ordenado por gasto no período "
                      "(gasto_periodo).")
        else:
            filtro = "Só itens ativos." if somente_ativos else "Ativos e pausados (sem arquivados)."
        partes = [f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}), moeda {acc['moeda']}. {filtro}"]
        partes += [f"AVISO: {a}" for a in avisos]
        for k in ("campanhas", "conjuntos", "anuncios"):
            if k in est:
                itens = est[k]
                faltando = 0
                if com_periodo:
                    for i in itens:
                        i["gasto_periodo"] = round(gasto[k].get(i.get(idcol[k]), 0), 2)
                    itens.sort(key=lambda i: -(i.get("gasto_periodo") or 0))
                    faltando = len(gasto[k]) - len(itens)
                partes.append(f"\n## {k}: {len(itens)}" + (f" (mostrando {max_linhas})" if len(itens) > max_linhas else "")
                              + (f" | {faltando} que veicularam no período já não aparecem (arquivados ou excluídos)" if faltando > 0 else ""))
                partes.append(json.dumps([_clean(i) for i in itens[:max_linhas]], ensure_ascii=False, separators=(",", ":")))
        return "\n".join(partes)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def consultar_insights(
    cliente: _CLIENTE = None,
    conta: Annotated[Optional[str], Field(description="ID da conta ou trecho do nome. Vazio usa a conta padrão.")] = None,
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias terminando ontem (se o cliente exige período, é obrigatório).")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD. Vazio: ontem (se o cliente exige período, é obrigatório).")] = None,
    nivel: Annotated[Literal["conta", "campanha", "conjunto", "anuncio"], Field(description="Nível das linhas.")] = "campanha",
    por_dia: Annotated[bool, Field(description="True quebra por dia; False devolve o período inteiro (alcance deduplicado no período).")] = False,
    quebra: Annotated[Literal["nenhuma", "idade_genero", "idade", "genero", "plataforma", "posicionamento",
                              "dispositivo", "regiao", "hora"], Field(description="Detalhamento.")] = "nenhuma",
    campanha_ids: Annotated[Optional[list[str]], Field(description="Filtra por IDs de campanha.")] = None,
    max_linhas: Annotated[int, Field(description="Teto de linhas na resposta (ordenadas por gasto).", ge=1, le=3000)] = 200,
    com_video: Annotated[bool, Field(description="True acrescenta as colunas de vídeo: video_plays, video_p25, "
                                                 "video_p50, video_p75, video_p100 e thruplays.")] = False,
) -> str:
    """Métricas de desempenho direto da Meta, em CSV: gasto, impressões, alcance, frequência, cliques
    no link, visualizações de página, carrinho, checkout, compras, valor de conversão, CTR, CPM, CPC,
    CPA, ROAS e ticket; com com_video, também reproduções, 25/50/75/100% do vídeo e ThruPlays. Usa a atribuição configurada em cada conjunto, como o Gerenciador. O bloco
    TOTAL soma só o que é somável; alcance e frequência nunca devem ser somados entre linhas."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _checar_periodo(desde, ate)
        d1, d2 = _periodo(desde, ate)
        level, bks = NIVEIS[nivel], QUEBRAS[quebra]
        avisos: list = []
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            rows = await _insights(c, acc["act"], level, d1, d2, por_dia, bks,
                                   asyncio.Semaphore(MAX_SIMULTANEAS), campanha_ids, avisos, com_video)
        total = _soma(rows)
        cols = []
        if por_dia:
            cols.append("dia")
        cols += [_BREAKDOWN_KEY[b] for b in bks]
        if level != "account":
            cols += ["campanha", "campanha_id", "objetivo"]
        if level in ("adset", "ad"):
            cols += ["conjunto", "conjunto_id"]
        if level == "ad":
            cols += ["anuncio", "anuncio_id"]
        cols += ["gasto", "impressoes", "alcance", "frequencia", "cliques_link", "lpv", "carrinho", "checkout",
                 "compras", "valor_compras", "ctr_link", "cpm", "cpc_link", "cpa", "roas", "ticket"]
        if com_video:
            cols += list(_VIDEO)
        if por_dia:
            rows.sort(key=lambda r: (r.get("dia") or "", -(r.get("gasto") or 0)))
            mostrar = rows[:max_linhas]
        else:
            rows.sort(key=lambda r: -(r.get("gasto") or 0))
            mostrar = rows[:max_linhas]
        head = [
            f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}) | {d1} a {d2} | nível {nivel} | "
            f"{'por dia' if por_dia else 'período inteiro'} | quebra {quebra}",
            f"Linhas: {len(rows)}" + (f" (mostrando {len(mostrar)}; para tudo use exportar_relatorio)" if len(rows) > len(mostrar) else ""),
            "TOTAL das linhas: " + _resumo_txt(total, acc["moeda"]),
        ]
        head += [f"AVISO: {a}" for a in avisos]
        return "\n".join(head) + "\n\n" + _csv(mostrar, cols)
    except Exception as e:
        return _tool_error(e)


@mcp.tool()
async def exportar_relatorio(
    cliente: _CLIENTE = None,
    conta: Annotated[Optional[str], Field(description="ID da conta ou trecho do nome. Vazio usa a conta padrão.")] = None,
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias terminando ontem (se o cliente exige período, é obrigatório).")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD. Vazio: ontem (se o cliente exige período, é obrigatório).")] = None,
    nivel: Annotated[Literal["anuncio", "conjunto", "campanha"], Field(description="Nível das Tabelas A e B. O prompt v4 decide por anúncio.")] = "anuncio",
    linha_de_resumo: Annotated[bool, Field(description="Inclui a linha de totais da conta (nomes em branco) no topo das Tabelas A e B, como no export do Gerenciador.")] = True,
) -> str:
    """Gera no computador (na pasta do cliente) um .xlsx com tudo que o prompt de análise de rifa (v4) pede:
    Tabela A (dia x anúncio x idade x gênero), Tabela B (dia x anúncio x plataforma), Tabela C sem
    dia (alcance deduplicado do período por campanha, conjunto e anúncio), conta por dia, e a
    estrutura real (orçamentos e limites de CPA). Devolve o caminho do arquivo, os totais da conta
    e a conferência de somas. Não altera nada na conta."""
    try:
        from openpyxl import Workbook

        nome_cliente = _usar_cliente(cliente)
        _checar_periodo(desde, ate)
        d1, d2 = _periodo(desde, ate)
        level = NIVEIS[nivel]
        avisos: list = []
        sem = asyncio.Semaphore(MAX_SIMULTANEAS)
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            act = acc["act"]
            (resumo, conta_dia, tab_a, tab_b, c_camp, c_conj, c_anun, est) = await asyncio.gather(
                _insights(c, act, "account", d1, d2, False, [], sem),
                _insights(c, act, "account", d1, d2, True, [], sem),
                _insights(c, act, level, d1, d2, True, ["age", "gender"], sem, avisos=avisos),
                _insights(c, act, level, d1, d2, True, ["publisher_platform"], sem, avisos=avisos),
                _insights(c, act, "campaign", d1, d2, False, [], sem, avisos=avisos),
                _insights(c, act, "adset", d1, d2, False, [], sem, avisos=avisos),
                _insights(c, act, "ad", d1, d2, False, [], sem, avisos=avisos),
                _estrutura(c, acc, {"campanhas", "conjuntos"}, False, sem),
            )
        if not resumo:
            return f"Cliente: {nome_cliente} | Sem veiculação na conta {acc['nome']} ({acc['id']}) entre {d1} e {d2}. Nenhum arquivo gerado."
        tot = resumo[0]
        moeda = acc["moeda"]
        ordem = lambda r: (r.get("dia") or "", r.get("campanha") or "", r.get("conjunto") or "", r.get("anuncio") or "")
        tab_a.sort(key=ordem)
        tab_b.sort(key=ordem)
        for t in (c_camp, c_conj, c_anun):
            t.sort(key=lambda r: -(r.get("gasto") or 0))

        resumo_row = dict(tot) if linha_de_resumo else None
        wb = Workbook(write_only=True)
        _sheet(wb, "Tabela A - Idade e Genero", _colunas_xlsx(level, True, ["age", "gender"], moeda), tab_a, resumo_row)
        _sheet(wb, "Tabela B - Plataforma", _colunas_xlsx(level, True, ["publisher_platform"], moeda), tab_b, resumo_row)
        _sheet(wb, "Tabela C - Campanhas", _colunas_xlsx("campaign", False, [], moeda), c_camp, resumo_row)
        _sheet(wb, "Tabela C - Conjuntos", _colunas_xlsx("adset", False, [], moeda), c_conj)
        _sheet(wb, "Tabela C - Anuncios", _colunas_xlsx("ad", False, [], moeda), c_anun)
        _sheet(wb, "Conta por dia", _colunas_xlsx("account", True, [], moeda), sorted(conta_dia, key=lambda r: r.get("dia") or ""))

        camp_nome = {x["campanha_id"]: x["campanha"] for x in est.get("campanhas", [])}
        cols_ec = [("Nome da campanha", "campanha"), ("Identificação da campanha", "campanha_id"), ("Objetivo", "objetivo"),
                   ("Status", "status"), ("Estratégia de lance", "estrategia_lance"),
                   (f"Orçamento diário ({moeda})", "orcamento_diario"), (f"Orçamento total ({moeda})", "orcamento_total"),
                   (f"Limite de gasto ({moeda})", "limite_gasto"), ("Criada em", "criada_em")]
        _sheet(wb, "Estrutura - Campanhas", cols_ec, est.get("campanhas", []))
        conj = [dict(x, campanha=camp_nome.get(x.get("campanha_id"))) for x in est.get("conjuntos", [])]
        cols_ej = [("Nome da campanha", "campanha"), ("Identificação da campanha", "campanha_id"),
                   ("Nome do conjunto de anúncios", "conjunto"), ("Identificação do conjunto de anúncios", "conjunto_id"),
                   ("Status", "status"), ("Estratégia de lance", "estrategia_lance"),
                   (f"Limite de CPA ou lance ({moeda})", "limite_cpa_ou_lance"), ("ROAS mínimo", "roas_minimo"),
                   (f"Orçamento diário ({moeda})", "orcamento_diario"), (f"Orçamento total ({moeda})", "orcamento_total"),
                   ("Otimização", "otimizacao"), ("Idade", "idade"), ("Gênero", "genero"), ("Países", "paises"),
                   ("Regiões", "regioes"), ("Nº de cidades", "cidades"), ("Públicos", "publicos"),
                   ("Públicos excluídos", "publicos_excluidos"), ("Advantage+ público", "advantage_publico"),
                   ("Criado em", "criado_em")]
        _sheet(wb, "Estrutura - Conjuntos", cols_ej, conj)

        # Conferência de somas (Camada 0 do prompt v4)
        sa, sb = _soma(tab_a), _soma(tab_b)

        def desvio(v, ref):
            return None if not ref else (v - ref) / ref * 100

        alc_linhas_a = sum(r.get("alcance") or 0 for r in tab_a)
        fator = alc_linhas_a / tot["alcance"] if tot.get("alcance") else None
        acao = _cfg("META_PURCHASE_ACTION")
        info = [
            ("Cliente", nome_cliente), ("Conta", f"{acc['nome']} ({acc['id']})"), ("Período", f"{d1} a {d2}"), ("Moeda", moeda),
            ("Fuso da conta", acc["fuso"]), ("Gerado em", datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("Fonte", f"Marketing API {_api_version()}, atribuição do conjunto (use_unified_attribution_setting)"),
            ("Evento de compra", acao if acao and acao.lower() != "auto" else "automático: omni_purchase > purchase > pixel"),
            ("Nível das Tabelas A e B", nivel),
            ("Linha de resumo nas Tabelas A, B e C-Campanhas", "sim, primeira linha, nomes em branco" if linha_de_resumo else "não"),
            ("Linhas Tabela A", len(tab_a)), ("Linhas Tabela B", len(tab_b)),
            (f"Gasto da conta ({moeda})", round(tot["gasto"], 2)),
            ("Desvio do gasto Tabela A vs conta (%)", _r(desvio(sa["gasto"], tot["gasto"]), 2)),
            ("Desvio do gasto Tabela B vs conta (%)", _r(desvio(sb["gasto"], tot["gasto"]), 2)),
            ("Desvio de compras Tabela A vs conta (%)", _r(desvio(sa["compras"], tot["compras"]), 2)),
            ("Alcance único da conta no período", tot.get("alcance")),
            ("Fator de superestimação do alcance (soma das linhas da Tabela A / alcance real)", _r(fator, 2)),
            ("Aviso", "Alcance e Frequência são deduplicados dentro da linha. Nunca somar entre linhas."),
        ] + [("Aviso", a) for a in dict.fromkeys(avisos)]
        ws = wb.create_sheet("Info")
        ws.column_dimensions["A"].width = 62
        ws.column_dimensions["B"].width = 70
        for k, v in info:
            ws.append([k, v])

        nome = f"meta-ads_{nome_cliente}_{_slug(acc['nome'])}_{d1}_a_{d2}.xlsx"
        destino = _export_dir() / nome
        wb.save(destino)

        top = "\n".join(
            f"- {r['campanha']} (ID {r['campanha_id']}, {r.get('objetivo')}): {moeda} {r['gasto']:,.2f}, "
            f"{r['compras']:.0f} compras, ROAS {(r['roas'] or 0):.2f}"
            + (f", freq. do período {r['frequencia']:.2f}" if r.get("frequencia") else "")
            for r in c_camp[:12]
        )

        def pct(v):
            return "n/d" if v is None else f"{round(v, 2) + 0.0:+.2f}%"  # + 0.0 evita "-0.00%"

        out = [
            f"Arquivo gerado: {destino}",
            f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}) | {d1} a {d2}",
            "RESUMO DA CONTA: " + _resumo_txt(tot, moeda),
            f"Linhas: Tabela A {len(tab_a)}, Tabela B {len(tab_b)}, campanhas {len(c_camp)}, "
            f"conjuntos {len(c_conj)}, anúncios {len(c_anun)}, dias {len(conta_dia)}.",
            f"Conferência de somas vs conta: gasto A {pct(desvio(sa['gasto'], tot['gasto']))}, "
            f"gasto B {pct(desvio(sb['gasto'], tot['gasto']))}, compras A {pct(desvio(sa['compras'], tot['compras']))}.",
        ]
        if fator:
            out.append(f"Fator de superestimação do alcance ao somar linhas da Tabela A: {fator:.2f}x.")
        out += [f"AVISO: {a}" for a in dict.fromkeys(avisos)]
        out.append(f"\nCampanhas por gasto (até 12 de {len(c_camp)}):\n{top}")
        return "\n".join(out)
    except Exception as e:
        return _tool_error(e)


# ───────────────────────── escrita (com permissão) ─────────────────────────

PERMISSOES = {
    "status": "ativar e pausar",
    "orcamento": "orçamentos e lances",
    "editar": "renomear e editar (segmentação, públicos, datas)",
    "criar": "criar e duplicar (sempre pausado)",
    "bm": "administrar a BM (dar acesso limitado a contas e páginas)",
}
_ESTRATEGIAS = {
    "menor_custo": "LOWEST_COST_WITHOUT_CAP",
    "limite_custo": "COST_CAP",
    "limite_lance": "LOWEST_COST_WITH_BID_CAP",
    "roas_minimo": "LOWEST_COST_WITH_MIN_ROAS",
}
_CTAS = Literal["LEARN_MORE", "SHOP_NOW", "BUY_NOW", "ORDER_NOW", "SIGN_UP", "GET_OFFER", "CONTACT_US", "SUBSCRIBE"]
_MUDA = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
_CRIA = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_IMAGENS_OK = {".jpg", ".jpeg", ".png", ".gif"}
_TIPO_PT = {"campaign": "campanha", "adset": "conjunto", "ad": "anúncio"}


def _parse_permissoes(texto: Optional[str]) -> list:
    itens = {x.strip().lower() for x in (texto or "").split(",")}
    return [p for p in PERMISSOES if p in itens]


def _exigir(permissao: str) -> None:
    if permissao not in _parse_permissoes(_cfg("PERMISSOES")):
        raise MetaError(
            f"O cliente '{_cliente_id()}' não permite {PERMISSOES[permissao]}. Para liberar, abra o app "
            "Impera Meta MCP, ficha do cliente, seção 'O que o Claude pode fazer'.")


def _numero(texto: str) -> Optional[float]:
    t = (texto or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _checar_teto(valor_dia: Optional[float], moeda: str) -> None:
    teto = _numero(_cfg("ORCAMENTO_MAX_DIA"))
    if valor_dia is not None and teto is not None and valor_dia > teto + 1e-9:
        raise MetaError(f"Orçamento diário de {moeda} {valor_dia:,.2f} passa do teto de {moeda} {teto:,.2f} definido "
                        f"para o cliente '{_cliente_id()}' no app. Nada foi alterado.")


def _centavos(valor: float, moeda: str) -> int:
    if valor is None or valor <= 0:
        raise MetaError("Valores de orçamento e lance precisam ser maiores que zero.")
    return int(round(valor * (1 if moeda in _ZERO_DECIMAL else 100)))


def _registrar(ferramenta: str, alvo: str, parametros: dict, ok: bool, resultado: str) -> None:
    """Histórico local de cada alteração, por cliente. Nunca contém token nem bytes de imagem."""
    try:
        HISTORICO_DIR.mkdir(parents=True, exist_ok=True)
        arq = HISTORICO_DIR / f"{_cliente_id() or 'padrao'}.jsonl"
        limpos = {k: (v if not isinstance(v, str) or len(v) < 400 else v[:400] + "…")
                  for k, v in parametros.items() if v not in (None, "", [], {}) and k != "bytes"}
        reg = {"quando": datetime.now().isoformat(timespec="seconds"), "ferramenta": ferramenta,
               "alvo": alvo, "parametros": limpos, "ok": ok, "resultado": resultado[:600]}
        if _CONTA_USADA.get():
            reg["conta"] = _CONTA_USADA.get()
        linha = json.dumps(reg, ensure_ascii=False)
        fd = os.open(str(arq), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(_scrub(linha) + "\n")
        if arq.stat().st_size > 2_000_000:
            linhas = arq.read_text(encoding="utf-8").splitlines()[-2000:]
            arq.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    except OSError:
        log.warning("não consegui gravar o histórico de ações")


def historico(cliente_id: str, limite: int = 30) -> list:
    arq = HISTORICO_DIR / f"{cliente_id}.jsonl"
    if not arq.exists():
        return []
    out = []
    for linha in arq.read_text(encoding="utf-8").splitlines()[-limite:]:
        try:
            out.append(json.loads(linha))
        except ValueError:
            pass
    return list(reversed(out))


def _friendly_escrita(code: Any, msg: str, status: int) -> str:
    msg = _scrub(msg)
    if code in (10, 200, 294) or (isinstance(code, int) and 200 <= code <= 299):
        return ("O token não tem permissão para alterar esta conta. Ele precisa de ads_management e o usuário "
                f"do sistema precisa ter acesso de anunciante à conta. Nada foi alterado. Detalhe: {msg}")
    if code == 190:
        return _friendly(code, msg, status)
    return f"A Meta recusou a alteração (código {code}): {msg}"


async def _post(client: httpx.AsyncClient, path: str, data: dict, metodo: str = "POST",
                arquivos: Optional[dict] = None) -> dict:
    """POST (ou DELETE, para tirar linhas de um público) na Graph API. `arquivos` envia um pedaço de
    arquivo em multipart (upload de vídeo). Só repete quando a Meta recusou antes de processar (limite
    de chamadas ou falha de conexão); em qualquer dúvida não repete, para nunca criar nada em dobro."""
    url = f"{GRAPH_HOST}/{_api_version()}/{path.lstrip('/')}"
    form = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v))
            for k, v in data.items() if v is not None}
    secret = _cfg("META_APP_SECRET")
    if secret:
        form["appsecret_proof"] = hmac.new(secret.encode(), _bearer(client).encode(), hashlib.sha256).hexdigest()
    for attempt in range(len(_BACKOFF) + 1):
        last = attempt == len(_BACKOFF)
        try:
            if arquivos:
                r = await client.request(metodo, url, data=form, files=arquivos)
            else:
                r = await client.request(metodo, url, data=form)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if not last:
                await asyncio.sleep(_BACKOFF[attempt])
                continue
            raise MetaError("Não consegui conectar à Meta. Nada foi enviado.")
        except httpx.HTTPError as e:
            raise MetaError(f"A conexão caiu durante o envio ({type(e).__name__}). A alteração PODE ter sido "
                            "aplicada: confira com estrutura_conta antes de repetir.")
        try:
            resposta = r.json()
        except ValueError:
            resposta = None
        if r.status_code == 200 and isinstance(resposta, dict) and "error" not in resposta:
            return resposta
        err = resposta.get("error", {}) if isinstance(resposta, dict) else {}
        code = err.get("code")
        msg = str(err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}")
        if err.get("error_user_title"):
            msg = f"{err['error_user_title']}: {msg}"
        if (code in _RATE_CODES or (isinstance(code, int) and 80000 <= code <= 80014)) and not last:
            await asyncio.sleep(_BACKOFF[attempt])
            continue
        if r.status_code >= 500 or code in (1, 2) or err.get("is_transient"):
            raise MetaError(f"A Meta teve uma falha temporária ({msg}). A alteração PODE ter sido aplicada: "
                            "confira com estrutura_conta antes de repetir.", code=code)
        raise MetaError(_friendly_escrita(code, msg, r.status_code), code=code)
    raise MetaError("Falha inesperada ao enviar para a Meta.")


def _so_digitos(v: Any) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _contas_liberadas() -> list:
    """IDs das contas onde o Claude pode alterar para o cliente ativo: a conta padrão primeiro,
    depois as marcadas em CONTAS_LIBERADAS no app. Qualquer outra conta é recusada."""
    out: list = []
    for x in [_cfg("META_AD_ACCOUNT_ID")] + re.split(r"[,;\s]+", _cfg("CONTAS_LIBERADAS")):
        d = _so_digitos(x)
        if d and d not in out:
            out.append(d)
    return out


async def _conta_padrao(c: httpx.AsyncClient, conta: Optional[str] = None) -> dict:
    """Conta onde a ferramenta vai trabalhar: a padrão do cliente ou, se informada em 'conta', outra
    que o cliente tenha liberado no app. Nunca uma conta fora da lista."""
    liberadas = _contas_liberadas()
    texto = (conta or "").strip()
    if texto and not re.fullmatch(r"(act_)?\d{6,}", texto):
        raise MetaError("Para trabalhar em outra conta, passe o ID numérico dela em 'conta' (veja listar_contas). "
                        "Nomes só valem nas consultas de leitura.")
    alvo = _so_digitos(texto) or _so_digitos(_cfg("META_AD_ACCOUNT_ID"))
    if not alvo:
        raise MetaError(f"O cliente '{_cliente_id()}' não tem conta padrão. Alterações só são feitas nas contas "
                        "liberadas: escolha a padrão na ficha do cliente no app.")
    if alvo not in liberadas:
        raise MetaError(f"A conta {alvo} não está liberada para alterações no cliente '{_cliente_id()}'. "
                        "Contas liberadas: " + (", ".join(liberadas) or "nenhuma")
                        + ". Para liberar, marque-a na ficha do cliente no app. Nada foi alterado.")
    info = await _get(c, f"act_{alvo}", {"fields": "account_id,name,currency"})
    _CONTA_USADA.set(alvo)
    return {"id": alvo, "act": f"act_{alvo}", "nome": info.get("name") or alvo, "moeda": info.get("currency") or "BRL"}


async def _objeto(c: httpx.AsyncClient, obj_id: str, campos: str = "") -> tuple:
    """Lê o objeto, descobre o tipo e garante que ele é de uma conta liberada do cliente."""
    if not re.fullmatch(r"\d{5,25}", str(obj_id)):
        raise MetaError(f"ID inválido: {obj_id!r}.")
    obj = await _get(c, str(obj_id), {"fields": ",".join(x for x in ("account_id,name", campos) if x), "metadata": "1"})
    dono = _so_digitos(obj.get("account_id"))
    if dono not in _contas_liberadas():
        padrao = await _conta_padrao(c)
        raise MetaError(f"O objeto {obj_id} é da conta {obj.get('account_id')}, que não é a conta padrão do cliente "
                        f"'{_cliente_id()}' ({padrao['nome']}, {padrao['id']}) nem uma conta liberada "
                        "(não da conta padrão ou das liberadas). Nada foi alterado.")
    conta = await _conta_padrao(c, dono)
    tipo = await _descobrir_tipo(c, str(obj_id), obj)
    return obj, tipo, conta


# Nomes que a Meta pode devolver em metadata.type (inclui os nomes internos antigos:
# campanha = adcampaign_group, conjunto = adcampaign, anúncio = adgroup).
_TIPOS_META = {
    "campaign": "campaign", "adcampaign_group": "campaign", "adcampaigngroup": "campaign",
    "adset": "adset", "ad_set": "adset", "adcampaign": "adset",
    "ad": "ad", "adgroup": "ad",
}
# Sondagem quando a Meta não manda metadata: cada campo só existe num tipo de objeto
# (adset_id só no anúncio; campaign_id no conjunto e no anúncio; objective só na campanha).
_SONDAS = (("adset_id", "ad"), ("campaign_id", "adset"), ("objective", "campaign"))


def _tipo_frase(tipo: str) -> str:
    return {"campaign": "de uma campanha", "adset": "de um conjunto", "ad": "de um anúncio"}.get(
        tipo, "de um tipo que não deu para identificar (confira se é campanha, conjunto ou anúncio)")


def _normalizar_tipo(valor: Any) -> str:
    return _TIPOS_META.get(re.sub(r"[^a-z_]", "", str(valor or "").lower()), "")


async def _descobrir_tipo(c: httpx.AsyncClient, obj_id: str, obj: dict) -> str:
    """Descobre se o ID é campanha, conjunto ou anúncio sem depender só do metadata da Meta,
    que em versões novas da API pode vir vazio ou com nomes antigos."""
    tipo = _normalizar_tipo((obj.get("metadata") or {}).get("type"))
    if tipo:
        return tipo
    for campo, candidato in _SONDAS:
        try:
            r = await _get(c, obj_id, {"fields": campo})
        except MetaError as e:
            if e.code != 100:
                raise
            achado = re.search(r"node type \((\w+)\)", str(e), re.I)
            if achado and _normalizar_tipo(achado.group(1)):
                return _normalizar_tipo(achado.group(1))
            continue
        if r.get(campo):
            return candidato
    return ""


def _ok_pausado(tipo: str, novo_id: str, nome: str) -> str:
    fem = tipo == "Campanha"
    return (f"{tipo} {'criada PAUSADA' if fem else 'criado PAUSADO'}: {nome} (ID {novo_id}). "
            "Para ativar, use alterar_status com acao='ativar'.")


@mcp.tool(annotations=_MUDA)
async def alterar_status(
    ids: Annotated[list[str], Field(description="IDs de campanhas, conjuntos ou anúncios (até 50).", min_length=1, max_length=50)],
    acao: Annotated[Literal["ativar", "pausar"], Field(description="ativar ou pausar.")],
    cliente: _CLIENTE = None,
) -> str:
    """Ativa ou pausa campanhas, conjuntos ou anúncios da conta padrão do cliente. Exige a permissão
    'ativar e pausar' no app. Confirme com o usuário antes de usar."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("status")
        status = "ACTIVE" if acao == "ativar" else "PAUSED"
        linhas = []
        async with _client() as c:
            for obj_id in ids:
                try:
                    obj, tipo, _ = await _objeto(c, obj_id, "effective_status")
                    await _post(c, str(obj_id), {"status": status})
                    msg = f"{_TIPO_PT.get(tipo, tipo or 'objeto')} {obj.get('name')} ({obj_id}): {obj.get('effective_status')} -> {status}"
                    _registrar("alterar_status", str(obj_id), {"acao": acao}, True, msg)
                    linhas.append("OK " + msg)
                except MetaError as e:
                    _registrar("alterar_status", str(obj_id), {"acao": acao}, False, str(e))
                    linhas.append(f"ERRO {obj_id}: {_scrub(str(e))}")
        return f"Cliente: {nome_cliente}\n" + "\n".join(linhas)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def alterar_orcamento(
    id: Annotated[str, Field(description="ID da campanha (orçamento de campanha) ou do conjunto.")],
    orcamento_diario: Annotated[Optional[float], Field(description="Novo orçamento diário, na moeda da conta (ex.: 150.00).", gt=0)] = None,
    orcamento_total: Annotated[Optional[float], Field(description="Novo orçamento total, na moeda da conta.", gt=0)] = None,
    limite_cpa: Annotated[Optional[float], Field(description="Novo limite de custo ou de lance do conjunto (bid_amount), na moeda da conta.", gt=0)] = None,
    roas_minimo: Annotated[Optional[float], Field(description="Novo ROAS mínimo do conjunto (ex.: 2.0), quando a estratégia é de ROAS mínimo.", gt=0)] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Altera orçamento diário/total de uma campanha ou conjunto, e limite de CPA ou ROAS mínimo de um
    conjunto, na conta padrão do cliente. Respeita o teto de orçamento diário do app. Exige a
    permissão 'orçamentos e lances'. Confirme com o usuário antes de usar."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("orcamento")
        if all(v is None for v in (orcamento_diario, orcamento_total, limite_cpa, roas_minimo)):
            raise MetaError("Informe ao menos um valor para alterar.")
        params = {"orcamento_diario": orcamento_diario, "orcamento_total": orcamento_total,
                  "limite_cpa": limite_cpa, "roas_minimo": roas_minimo}
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, id)
            if tipo not in ("campaign", "adset"):
                raise MetaError(f"Orçamento é de campanha ou conjunto; este ID é {_tipo_frase(tipo)}.")
            obj.update(await _get(c, str(id), {"fields": "daily_budget,lifetime_budget"}))
            moeda = conta["moeda"]
            if (limite_cpa is not None or roas_minimo is not None) and tipo != "adset":
                raise MetaError(f"Limite de CPA e ROAS mínimo são do conjunto de anúncios; este ID é {_tipo_frase(tipo)}.")
            _checar_teto(orcamento_diario, moeda)
            dados: dict = {}
            antes = []
            if orcamento_diario is not None:
                dados["daily_budget"] = _centavos(orcamento_diario, moeda)
                antes.append(f"diário {moeda} {(_minor(obj.get('daily_budget'), moeda) or 0):,.2f} -> {orcamento_diario:,.2f}")
            if orcamento_total is not None:
                dados["lifetime_budget"] = _centavos(orcamento_total, moeda)
                antes.append(f"total {moeda} {(_minor(obj.get('lifetime_budget'), moeda) or 0):,.2f} -> {orcamento_total:,.2f}")
            if limite_cpa is not None:
                dados["bid_amount"] = _centavos(limite_cpa, moeda)
                antes.append(f"limite de CPA -> {moeda} {limite_cpa:,.2f}")
            if roas_minimo is not None:
                dados["bid_constraints"] = {"roas_average_floor": int(round(roas_minimo * 10000))}
                antes.append(f"ROAS mínimo -> {roas_minimo:.2f}")
            try:
                await _post(c, str(id), dados)
            except MetaError as e:
                _registrar("alterar_orcamento", str(id), params, False, str(e))
                raise
        msg = f"{_TIPO_PT.get(tipo, tipo)} {obj.get('name')} ({id}): " + "; ".join(antes)
        _registrar("alterar_orcamento", str(id), params, True, msg)
        return f"Cliente: {nome_cliente}\nOK {msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def renomear(
    id: Annotated[str, Field(description="ID da campanha, conjunto ou anúncio.")],
    nome: Annotated[str, Field(description="Nome novo.", min_length=1, max_length=400)],
    cliente: _CLIENTE = None,
) -> str:
    """Renomeia uma campanha, conjunto ou anúncio da conta padrão do cliente. Exige a permissão 'renomear'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("editar")
        async with _client() as c:
            obj, tipo, _ = await _objeto(c, id)
            try:
                await _post(c, str(id), {"name": nome})
            except MetaError as e:
                _registrar("renomear", str(id), {"nome": nome}, False, str(e))
                raise
        msg = f"{_TIPO_PT.get(tipo, tipo)} {id}: '{obj.get('name')}' -> '{nome}'"
        _registrar("renomear", str(id), {"nome": nome}, True, msg)
        return f"Cliente: {nome_cliente}\nOK {msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def duplicar(
    id: Annotated[str, Field(description="ID da campanha, conjunto ou anúncio a duplicar.")],
    novo_nome: Annotated[Optional[str], Field(description="Nome da cópia. Vazio: nome original + ' - cópia'.")] = None,
    quantidade: Annotated[int, Field(description="Quantas cópias (1 a 5).", ge=1, le=5)] = 1,
    orcamento_diario: Annotated[Optional[float], Field(
        description="Orçamento diário de cada cópia, na moeda da conta. Vale para conjunto e para campanha com "
                    "orçamento de campanha. Informado, o teto do app é conferido contra este valor, e não "
                    "contra o do original.", gt=0)] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Duplica uma campanha (com conjuntos e anúncios), um conjunto (com anúncios) ou um anúncio, na conta
    padrão do cliente. As cópias nascem PAUSADAS, com o mesmo público, criativos e URLs. Com
    orcamento_diario, cada cópia já fica com o orçamento novo. Exige a permissão 'criar e duplicar'
    (e 'orçamentos e lances' quando muda o orçamento)."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if orcamento_diario is not None:
            _exigir("orcamento")
        params = {"novo_nome": novo_nome, "quantidade": quantidade, "orcamento_diario": orcamento_diario}
        linhas = []
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, id)
            moeda = conta["moeda"]
            if tipo not in ("campaign", "adset", "ad"):
                raise MetaError(f"Só dá para duplicar campanha, conjunto ou anúncio (este ID é {_tipo_frase(tipo)}).")
            if orcamento_diario is not None and tipo == "ad":
                raise MetaError("Anúncio não tem orçamento próprio. Duplique sem orcamento_diario. Nada foi criado.")
            if tipo in ("campaign", "adset"):
                orc = await _get(c, str(id), {"fields": "daily_budget,lifetime_budget"})
                diario = _minor(orc.get("daily_budget"), moeda)
                if orcamento_diario is not None:
                    if orc.get("lifetime_budget") and not orc.get("daily_budget"):
                        raise MetaError("O original usa orçamento total, não diário. Duplique sem orcamento_diario e "
                                        "ajuste com alterar_orcamento(orcamento_total=...). Nada foi criado.")
                    if tipo == "campaign" and not orc.get("daily_budget"):
                        raise MetaError("Esta campanha não tem orçamento de campanha: os orçamentos ficam nos conjuntos. "
                                        "Duplique sem orcamento_diario e ajuste os conjuntos da cópia com "
                                        "alterar_orcamento. Nada foi criado.")
                    _checar_teto(orcamento_diario, moeda)
                elif diario is not None:
                    _checar_teto(diario, moeda)
                elif tipo == "campaign":
                    # Orçamento nos conjuntos: a cópia herda o de cada um, então o teto vale para todos eles.
                    conjs = await _get_all(c, f"{id}/adsets", {"fields": "daily_budget", "limit": 100})
                    maior = max((_minor(x.get("daily_budget"), moeda) or 0 for x in conjs), default=0)
                    _checar_teto(maior or None, moeda)
            chave = {"campaign": "copied_campaign_id", "adset": "copied_adset_id", "ad": "copied_ad_id"}[tipo]
            pendente = False
            for n in range(1, quantidade + 1):
                dados: dict = {"status_option": "PAUSED", "rename_options": {"rename_suffix": " - cópia"}}
                if tipo in ("campaign", "adset"):
                    dados["deep_copy"] = True
                try:
                    r = await _post(c, f"{id}/copies", dados)
                except MetaError as e:
                    _registrar("duplicar", str(id), params, False, str(e))
                    if linhas:
                        linhas.append(f"ERRO na cópia {n}: {_scrub(str(e))}")
                        break
                    raise
                novo = str(r.get(chave) or "")
                filhos = len(r.get("ad_object_ids") or [])
                msg = (f"cópia {n} de {_TIPO_PT.get(tipo, tipo)} {obj.get('name')} ({id}) -> ID {novo}"
                       + (f", {filhos} objetos internos" if filhos else "") + ", PAUSADA")
                problemas = []
                if novo and novo_nome:
                    nome_final = novo_nome if quantidade == 1 else f"{novo_nome} {n}"
                    try:
                        await _post(c, novo, {"name": nome_final})
                        msg += f", nome '{nome_final}'"
                    except MetaError as e:
                        problemas.append(f"não renomeei ({_scrub(str(e))})")
                if novo and orcamento_diario is not None:
                    try:
                        await _post(c, novo, {"daily_budget": _centavos(orcamento_diario, moeda)})
                        msg += f", orçamento diário {moeda} {orcamento_diario:,.2f}"
                    except MetaError as e:
                        pendente = True
                        problemas.append(f"o orçamento NÃO foi trocado e a cópia está com o do original "
                                         f"({moeda} {diario or 0:,.2f}): {_scrub(str(e))}")
                if problemas:
                    msg += ". ATENÇÃO: " + "; ".join(problemas)
                _registrar("duplicar", str(id), params, not problemas, msg)
                linhas.append(("OK " if not problemas else "PARCIAL ") + msg)
        fim = "\nPara ativar, use alterar_status com acao='ativar'."
        if pendente:
            fim = ("\nAntes de ativar, ajuste o orçamento das cópias marcadas como PARCIAL com alterar_orcamento."
                   + fim)
        return f"Cliente: {nome_cliente}\n" + "\n".join(linhas) + fim
    except Exception as e:
        return _tool_error(e)


_CATEGORIAS_ESPECIAIS = Literal["EMPLOYMENT", "HOUSING", "FINANCIAL_PRODUCTS_SERVICES", "ISSUES_ELECTIONS_POLITICS",
                                "ONLINE_GAMBLING_AND_GAMING"]
_DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")
# Posicionamentos manuais: nome amigável -> (plataforma, campo de posições, posição na Meta).
_POSICOES = {
    "facebook_feed": ("facebook", "facebook_positions", "feed"),
    "facebook_stories": ("facebook", "facebook_positions", "story"),
    "facebook_reels": ("facebook", "facebook_positions", "facebook_reels"),
    "facebook_marketplace": ("facebook", "facebook_positions", "marketplace"),
    "facebook_coluna_direita": ("facebook", "facebook_positions", "right_hand_column"),
    "instagram_feed": ("instagram", "instagram_positions", "stream"),
    "instagram_stories": ("instagram", "instagram_positions", "story"),
    "instagram_reels": ("instagram", "instagram_positions", "reels"),
    "instagram_explorar": ("instagram", "instagram_positions", "explore"),
    "messenger_caixa_de_entrada": ("messenger", "messenger_positions", "messenger_home"),
    "audience_network": ("audience_network", "audience_network_positions", "classic"),
}
_POSICAO = Literal["facebook_feed", "facebook_stories", "facebook_reels", "facebook_marketplace", "facebook_coluna_direita",
                   "instagram_feed", "instagram_stories", "instagram_reels", "instagram_explorar",
                   "messenger_caixa_de_entrada", "audience_network"]
_DESTINO = Literal["site", "whatsapp", "messenger", "instagram_direct", "formulario", "perfil_instagram"]
_META_OTIMIZACAO = Literal["OFFSITE_CONVERSIONS", "VALUE", "LANDING_PAGE_VIEWS", "LINK_CLICKS", "REACH", "IMPRESSIONS",
                           "THRUPLAY", "CONVERSATIONS"]
# Campos avançados que podem ir num conjunto sem passar pelos parâmetros próprios. Só os que limitam ou
# refinam a entrega: nada que aumente gasto além do teto do cliente.
_EXTRAS_CONJUNTO = {"frequency_control_specs", "attribution_spec", "adset_schedule", "pacing_type", "dsa_beneficiary",
                    "dsa_payor", "is_dynamic_creative", "daily_spend_cap", "lifetime_spend_cap"}


def _data_meta(valor: str, campo: str, fim: bool = False) -> str:
    """Aceita AAAA-MM-DD ou AAAA-MM-DDTHH:MM e devolve ISO sem fuso (a Meta usa o fuso da conta)."""
    v = (valor or "").strip()
    if not _DATA_RE.match(v):
        raise MetaError(f"{campo} precisa estar no formato AAAA-MM-DD (ou AAAA-MM-DDTHH:MM), por exemplo 2026-11-30.")
    if "T" not in v:
        v += "T23:59:00" if fim else "T00:00:00"
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise MetaError(f"{campo} não é uma data válida: {valor!r}.")
    return v


def _dias_de_veiculacao(inicio: Optional[str], fim: str) -> int:
    d0 = datetime.fromisoformat(inicio).date() if inicio else date.today()
    d1 = datetime.fromisoformat(fim).date()
    if d1 < d0:
        raise MetaError("A data final é anterior à data inicial (ou a hoje).")
    return (d1 - d0).days + 1


def _checar_total(total: float, inicio: Optional[str], fim: str, moeda: str) -> None:
    """O teto do cliente é diário: um orçamento total vale pelo equivalente por dia."""
    dias = _dias_de_veiculacao(inicio, fim)
    try:
        _checar_teto(total / dias, moeda)
    except MetaError as e:
        raise MetaError(f"Orçamento total de {moeda} {total:,.2f} em {dias} dia(s) equivale a {moeda} {total / dias:,.2f} "
                        f"por dia. {e}")


def _id_interesse(valor: Any) -> str:
    v = str(valor).strip()
    if not re.fullmatch(r"\d{5,20}", v):
        raise MetaError(f"'{valor}' não é um ID de interesse da Meta. Use buscar_interesses para obter os IDs.")
    return v


def _geo(paises: list, regioes: list, cidades: list, raio_km: Optional[int]) -> dict:
    geo: dict = {"location_types": ["home", "recent"]}
    if cidades:
        geo["cities"] = [dict({"key": str(k)}, **({"radius": raio_km, "distance_unit": "kilometer"} if raio_km else {})) for k in cidades]
    if regioes:
        geo["regions"] = [{"key": str(k)} for k in regioes]
    if not cidades and not regioes:
        geo["countries"] = [p.upper() for p in paises] or ["BR"]
    return geo


def _aplicar_posicoes(alvo: dict, posicoes: list) -> None:
    plataformas: list = []
    campos: dict = {}
    for p in posicoes:
        plat, campo, valor = _POSICOES[p]
        if plat not in plataformas:
            plataformas.append(plat)
        if valor not in campos.setdefault(campo, []):
            campos[campo].append(valor)
    alvo["publisher_platforms"] = plataformas
    alvo.update(campos)


def _montar_alvo(*, paises: list, regioes: list, cidades: list, raio_km: Optional[int], idade_min: int, idade_max: int,
                 genero: str, publico_advantage: bool, incluir: list, excluir: list, interesses: list, posicoes: list) -> dict:
    if idade_min > idade_max:
        raise MetaError("A idade mínima não pode ser maior que a máxima.")
    alvo: dict = {"geo_locations": _geo(paises, regioes, cidades, raio_km), "age_min": idade_min, "age_max": idade_max,
                  "targeting_automation": {"advantage_audience": 1 if publico_advantage else 0}}
    if genero != "todos":
        alvo["genders"] = [1] if genero == "homens" else [2]
    if incluir:
        alvo["custom_audiences"] = [{"id": str(i)} for i in incluir]
    if excluir:
        alvo["excluded_custom_audiences"] = [{"id": str(i)} for i in excluir]
    if interesses:
        alvo["flexible_spec"] = [{"interests": [{"id": _id_interesse(i)} for i in interesses]}]
    if posicoes:
        _aplicar_posicoes(alvo, posicoes)
    return alvo


async def _publicos_da_conta(c: httpx.AsyncClient, conta: dict, ids: list) -> list:
    """Confere que cada público existe e é da conta padrão do cliente antes de usá-lo."""
    achados = []
    for i in dict.fromkeys(str(x).strip() for x in ids):
        if not re.fullmatch(r"\d{5,25}", i):
            raise MetaError(f"ID de público inválido: {i!r}. Use listar_publicos para ver os IDs.")
        a = await _get(c, i, {"fields": "id,name,account_id,subtype"})
        if str(a.get("account_id")) != conta["id"]:
            raise MetaError(f"O público {i} ('{a.get('name')}') não é da conta padrão do cliente '{_cliente_id()}' "
                            f"({conta['nome']}). Nada foi alterado.")
        achados.append(a)
    return achados


async def _pagina_da_conta(c: httpx.AsyncClient, conta: dict, pagina_id: Optional[str]) -> str:
    if not pagina_id or not re.fullmatch(r"\d{5,25}", str(pagina_id)):
        raise MetaError("Informe pagina_id (veja listar_paginas): este destino precisa de uma página do Facebook.")
    paginas = await _get_all(c, f"{conta['act']}/promote_pages", {"fields": "id,name", "limit": 100})
    if str(pagina_id) not in {str(p.get("id")) for p in paginas}:
        raise MetaError(f"A página {pagina_id} não está liberada para anunciar nesta conta. Páginas disponíveis: "
                        + ("; ".join(f"{p.get('name')} ({p.get('id')})" for p in paginas) or "nenhuma") + ".")
    return str(pagina_id)


def _extras(texto: Optional[str], permitidos: set) -> dict:
    if not texto:
        return {}
    try:
        d = json.loads(texto)
    except ValueError:
        raise MetaError("campos_extras precisa ser um JSON válido (ex.: {\"frequency_control_specs\": [...]}).")
    if not isinstance(d, dict):
        raise MetaError("campos_extras precisa ser um objeto JSON.")
    fora = sorted(set(d) - permitidos)
    if fora:
        raise MetaError(f"Campos não permitidos em campos_extras: {', '.join(fora)}. Permitidos: {', '.join(sorted(permitidos))}.")
    return d


@mcp.tool(annotations=_CRIA)
async def criar_campanha(
    nome: Annotated[str, Field(description="Nome da campanha.", min_length=1, max_length=400)],
    objetivo: Annotated[Literal["OUTCOME_SALES", "OUTCOME_LEADS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_AWARENESS"],
                        Field(description="Objetivo da campanha.")] = "OUTCOME_SALES",
    orcamento_diario: Annotated[Optional[float], Field(description="Orçamento diário da campanha (CBO / Advantage+). Vazio: orçamento fica nos conjuntos.", gt=0)] = None,
    estrategia_lance: Annotated[Literal["menor_custo", "limite_custo", "limite_lance", "roas_minimo"],
                                Field(description="Estratégia de lance quando o orçamento é da campanha.")] = "menor_custo",
    orcamento_total: Annotated[Optional[float], Field(description="Orçamento total (vitalício) da campanha, em vez do diário. Exige data_fim. O teto do cliente vale pelo equivalente por dia.", gt=0)] = None,
    limite_gasto: Annotated[Optional[float], Field(description="Limite de gasto da campanha (spend cap), na moeda da conta. Exige a permissão de orçamentos e lances.", gt=0)] = None,
    data_inicio: Annotated[Optional[str], Field(description="Início da campanha (AAAA-MM-DD ou AAAA-MM-DDTHH:MM, no fuso da conta).")] = None,
    data_fim: Annotated[Optional[str], Field(description="Fim da campanha (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")] = None,
    categorias_especiais: Annotated[list[_CATEGORIAS_ESPECIAIS], Field(
        description="Categorias especiais de anúncio (ex.: ONLINE_GAMBLING_AND_GAMING para sorteios/jogos, que a Meta só aceita "
                    "com autorização prévia). Vazio: nenhuma. Não dá para mudar depois de criada.")] = [],
    paises_categoria_especial: Annotated[list[str], Field(
        description="Países onde a categoria especial vale (ex.: ['BR']). Obrigatório com categorias_especiais; todo país "
                    "segmentado nos conjuntos precisa estar aqui.")] = [],
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria uma campanha PAUSADA na conta padrão do cliente: objetivo, orçamento diário ou total (CBO), estratégia de lance,
    limite de gasto, datas e categoria especial de anúncio. Exige a permissão 'criar e duplicar'. Depois crie os
    conjuntos com criar_conjunto e os anúncios com criar_anuncio."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if limite_gasto is not None:
            _exigir("orcamento")
        if orcamento_diario is not None and orcamento_total is not None:
            raise MetaError("Escolha orcamento_diario ou orcamento_total, não os dois.")
        if orcamento_total is not None and not data_fim:
            raise MetaError("Orçamento total exige data_fim (a Meta distribui o valor até essa data).")
        if categorias_especiais and not paises_categoria_especial:
            raise MetaError("Com categorias_especiais, informe paises_categoria_especial (ex.: ['BR']).")
        inicio = _data_meta(data_inicio, "data_inicio") if data_inicio else None
        fim = _data_meta(data_fim, "data_fim", fim=True) if data_fim else None
        if inicio and fim and inicio > fim:
            raise MetaError("data_fim é anterior à data_inicio.")
        params = {"nome": nome, "objetivo": objetivo, "orcamento_diario": orcamento_diario, "estrategia_lance": estrategia_lance,
                  "orcamento_total": orcamento_total, "limite_gasto": limite_gasto, "data_inicio": data_inicio,
                  "data_fim": data_fim, "categorias_especiais": categorias_especiais,
                  "paises_categoria_especial": paises_categoria_especial}
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            moeda = conta["moeda"]
            _checar_teto(orcamento_diario, moeda)
            if orcamento_total is not None:
                _checar_total(orcamento_total, inicio, fim, moeda)
            dados: dict = {"name": nome, "objective": objetivo, "status": "PAUSED",
                           "special_ad_categories": list(categorias_especiais), "buying_type": "AUCTION"}
            if categorias_especiais:
                dados["special_ad_category_country"] = [p.upper() for p in paises_categoria_especial]
            if orcamento_diario is not None:
                dados["daily_budget"] = _centavos(orcamento_diario, moeda)
                dados["bid_strategy"] = _ESTRATEGIAS[estrategia_lance]
            elif orcamento_total is not None:
                dados["lifetime_budget"] = _centavos(orcamento_total, moeda)
                dados["bid_strategy"] = _ESTRATEGIAS[estrategia_lance]
            else:
                dados["is_adset_budget_sharing_enabled"] = False
            if limite_gasto is not None:
                dados["spend_cap"] = _centavos(limite_gasto, moeda)
            if inicio:
                dados["start_time"] = inicio
            if fim:
                dados["stop_time"] = fim
            try:
                r = await _post(c, f"{conta['act']}/campaigns", dados)
            except MetaError as e:
                _registrar("criar_campanha", conta["act"], params, False, str(e))
                raise
        msg = _ok_pausado("Campanha", str(r.get("id")), nome)
        if "ONLINE_GAMBLING_AND_GAMING" in categorias_especiais:
            msg += (" Atenção: a Meta só aceita anúncios de jogos e sorteios online de contas autorizadas; sem a autorização "
                    "prévia os anúncios são reprovados.")
        _registrar("criar_campanha", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


async def _perfil_conjunto(c: httpx.AsyncClient, conta: dict, objetivo: Optional[str], destino: str, meta: Optional[str],
                           pixel_id: Optional[str], evento: str, pagina_id: Optional[str]) -> dict:
    """Define meta de otimização, destino e objeto promovido do conjunto conforme o objetivo da campanha."""
    campos: dict = {"billing_event": "IMPRESSIONS"}
    if destino == "site":
        if meta is None:
            meta = {"OUTCOME_SALES": "OFFSITE_CONVERSIONS", "OUTCOME_LEADS": "OFFSITE_CONVERSIONS",
                    "OUTCOME_TRAFFIC": "LANDING_PAGE_VIEWS", "OUTCOME_AWARENESS": "REACH"}.get(objetivo or "")
            if meta is None:
                raise MetaError(f"Para uma campanha {objetivo}, escolha o destino (whatsapp, messenger, instagram_direct, "
                                "formulario, perfil_instagram) ou uma meta_otimizacao; ou duplique um conjunto existente.")
        if meta in ("OFFSITE_CONVERSIONS", "VALUE"):
            if not pixel_id:
                pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
                if len(pixels) != 1:
                    raise MetaError("Informe o pixel_id. Pixels da conta: " + ("; ".join(f"{p.get('name')} ({p.get('id')})" for p in pixels) or "nenhum"))
                pixel_id = str(pixels[0]["id"])
            campos.update(optimization_goal=meta, destination_type="WEBSITE",
                          promoted_object={"pixel_id": str(pixel_id), "custom_event_type": evento})
        elif meta in ("LANDING_PAGE_VIEWS", "LINK_CLICKS"):
            campos.update(optimization_goal=meta, destination_type="WEBSITE")
        elif meta in ("REACH", "IMPRESSIONS", "THRUPLAY"):
            campos.update(optimization_goal=meta)
        else:
            raise MetaError(f"A meta {meta} só vale com destino de mensagens (whatsapp, messenger, instagram_direct).")
    elif destino in ("whatsapp", "messenger", "instagram_direct"):
        if meta not in (None, "CONVERSATIONS"):
            raise MetaError("Destinos de mensagem otimizam para CONVERSATIONS.")
        pagina = await _pagina_da_conta(c, conta, pagina_id)
        campos.update(optimization_goal="CONVERSATIONS", promoted_object={"page_id": pagina},
                      destination_type={"whatsapp": "WHATSAPP", "messenger": "MESSENGER", "instagram_direct": "INSTAGRAM_DIRECT"}[destino])
    elif destino == "formulario":
        if objetivo != "OUTCOME_LEADS":
            raise MetaError("Formulário instantâneo (lead ad) só vale em campanha de leads (OUTCOME_LEADS).")
        pagina = await _pagina_da_conta(c, conta, pagina_id)
        campos.update(optimization_goal="LEAD_GENERATION", destination_type="ON_AD", promoted_object={"page_id": pagina})
    elif destino == "perfil_instagram":
        if objetivo != "OUTCOME_TRAFFIC":
            raise MetaError("Visitas ao perfil do Instagram só valem em campanha de tráfego (OUTCOME_TRAFFIC).")
        campos.update(optimization_goal="VISIT_INSTAGRAM_PROFILE", destination_type="INSTAGRAM_PROFILE")
    return campos


@mcp.tool(annotations=_CRIA)
async def criar_conjunto(
    campanha_id: Annotated[str, Field(description="ID da campanha onde o conjunto será criado.")],
    nome: Annotated[str, Field(description="Nome do conjunto.", min_length=1, max_length=400)],
    pixel_id: Annotated[Optional[str], Field(description="Pixel/dataset para otimizar (vendas e leads no site). Vazio: usa o único pixel da conta, se houver só um.")] = None,
    evento: Annotated[Literal["PURCHASE", "LEAD", "COMPLETE_REGISTRATION", "INITIATED_CHECKOUT", "ADD_TO_CART", "CONTENT_VIEW"],
                      Field(description="Evento de conversão a otimizar.")] = "PURCHASE",
    orcamento_diario: Annotated[Optional[float], Field(description="Orçamento diário do conjunto. Obrigatório (ou orcamento_total) se a campanha não tiver orçamento.", gt=0)] = None,
    orcamento_total: Annotated[Optional[float], Field(description="Orçamento total (vitalício) do conjunto. Exige data_fim. O teto do cliente vale pelo equivalente por dia.", gt=0)] = None,
    estrategia_lance: Annotated[Optional[Literal["menor_custo", "limite_custo", "limite_lance", "roas_minimo"]],
                                Field(description="Só quando o orçamento é do conjunto. Vazio: menor custo.")] = None,
    limite_cpa: Annotated[Optional[float], Field(description="Limite de custo/lance, se a estratégia pedir.", gt=0)] = None,
    roas_minimo: Annotated[Optional[float], Field(description="ROAS mínimo, se a estratégia for de ROAS mínimo.", gt=0)] = None,
    paises: Annotated[list[str], Field(description="Códigos de país (ex.: ['BR']). Ignorado se houver regiões ou cidades.")] = ["BR"],
    regioes: Annotated[list[str], Field(description="Chaves de estados/regiões (use buscar_localizacao).")] = [],
    cidades: Annotated[list[str], Field(description="Chaves de cidades (use buscar_localizacao).")] = [],
    raio_km: Annotated[Optional[int], Field(description="Raio em km em volta das cidades (10 a 80).", ge=10, le=80)] = None,
    idade_min: Annotated[int, Field(ge=18, le=65)] = 18,
    idade_max: Annotated[int, Field(ge=18, le=65)] = 65,
    genero: Annotated[Literal["todos", "homens", "mulheres"], Field()] = "todos",
    publico_advantage: Annotated[bool, Field(description="Público Advantage+ (a Meta pode expandir além da segmentação).")] = True,
    publicos_incluir: Annotated[list[str], Field(description="IDs de públicos personalizados/semelhantes da conta para incluir (veja listar_publicos).")] = [],
    publicos_excluir: Annotated[list[str], Field(description="IDs de públicos da conta para excluir (ex.: quem já comprou).")] = [],
    interesses: Annotated[list[str], Field(description="IDs de interesses (obtenha com buscar_interesses; nunca invente IDs).")] = [],
    posicionamentos: Annotated[list[_POSICAO], Field(description="Posicionamentos manuais. Vazio: Advantage+ (automático).")] = [],
    destino: Annotated[_DESTINO, Field(description="Onde a pessoa converte: site (padrão), whatsapp, messenger, instagram_direct, formulario (lead ad) ou perfil_instagram.")] = "site",
    meta_otimizacao: Annotated[Optional[_META_OTIMIZACAO], Field(description="Meta de otimização. Vazio: a padrão do objetivo e destino (conversões, visualizações da página, alcance, conversas ou leads).")] = None,
    pagina_id: Annotated[Optional[str], Field(description="Página do Facebook (veja listar_paginas). Obrigatória em whatsapp, messenger, instagram_direct e formulario.")] = None,
    data_inicio: Annotated[Optional[str], Field(description="Início (AAAA-MM-DD ou AAAA-MM-DDTHH:MM, no fuso da conta). Vazio: ao ativar.")] = None,
    data_fim: Annotated[Optional[str], Field(description="Fim (AAAA-MM-DD ou AAAA-MM-DDTHH:MM). Obrigatório com orcamento_total.")] = None,
    campos_extras: Annotated[Optional[str], Field(description="JSON com campos avançados permitidos: " + ", ".join(sorted(_EXTRAS_CONJUNTO))
                                                   + " (limite de frequência, janela de atribuição, horários etc.).")] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Cria um conjunto de anúncios PAUSADO numa campanha da conta padrão do cliente: otimização (conversões no pixel,
    visualização da página, cliques, alcance, conversas no WhatsApp/Messenger/Direct, leads por formulário ou visitas ao
    perfil), segmentação (local, idade, gênero, públicos, exclusões, interesses, posicionamentos), orçamento diário ou
    total, lance e datas. Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if orcamento_diario is not None and orcamento_total is not None:
            raise MetaError("Escolha orcamento_diario ou orcamento_total, não os dois.")
        if orcamento_total is not None and not data_fim:
            raise MetaError("Orçamento total exige data_fim.")
        inicio = _data_meta(data_inicio, "data_inicio") if data_inicio else None
        fim = _data_meta(data_fim, "data_fim", fim=True) if data_fim else None
        if inicio and fim and inicio > fim:
            raise MetaError("data_fim é anterior à data_inicio.")
        extras = _extras(campos_extras, _EXTRAS_CONJUNTO)
        params = {k: v for k, v in dict(campanha_id=campanha_id, nome=nome, pixel_id=pixel_id, evento=evento,
                  orcamento_diario=orcamento_diario, orcamento_total=orcamento_total, estrategia_lance=estrategia_lance,
                  limite_cpa=limite_cpa, roas_minimo=roas_minimo, paises=paises, regioes=regioes, cidades=cidades,
                  raio_km=raio_km, idade_min=idade_min, idade_max=idade_max, genero=genero,
                  publico_advantage=publico_advantage, publicos_incluir=publicos_incluir, publicos_excluir=publicos_excluir,
                  interesses=interesses, posicionamentos=posicionamentos, destino=destino, meta_otimizacao=meta_otimizacao,
                  pagina_id=pagina_id, data_inicio=data_inicio, data_fim=data_fim, campos_extras=campos_extras).items()}
        alvo = _montar_alvo(paises=paises, regioes=regioes, cidades=cidades, raio_km=raio_km, idade_min=idade_min,
                            idade_max=idade_max, genero=genero, publico_advantage=publico_advantage, incluir=publicos_incluir,
                            excluir=publicos_excluir, interesses=interesses, posicoes=posicionamentos)
        async with _client() as c:
            camp, tipo, conta = await _objeto(c, campanha_id, "objective,daily_budget,lifetime_budget,bid_strategy")
            if tipo != "campaign":
                raise MetaError(f"{campanha_id} não é uma campanha.")
            moeda = conta["moeda"]
            dados: dict = {"name": nome, "campaign_id": str(campanha_id), "status": "PAUSED"}
            dados.update(await _perfil_conjunto(c, conta, camp.get("objective"), destino, meta_otimizacao, pixel_id, evento, pagina_id))
            if publicos_incluir or publicos_excluir:
                await _publicos_da_conta(c, conta, list(publicos_incluir) + list(publicos_excluir))
            cbo = bool(camp.get("daily_budget") or camp.get("lifetime_budget"))
            estrategia = camp.get("bid_strategy") if cbo else _ESTRATEGIAS[estrategia_lance or "menor_custo"]
            if cbo and (orcamento_diario is not None or orcamento_total is not None):
                raise MetaError("Esta campanha tem orçamento próprio (CBO); o conjunto não leva orçamento. Ajuste o da campanha.")
            if not cbo:
                if orcamento_diario is None and orcamento_total is None:
                    raise MetaError("A campanha não tem orçamento: informe orcamento_diario (ou orcamento_total) para o conjunto.")
                if orcamento_diario is not None:
                    _checar_teto(orcamento_diario, moeda)
                    dados["daily_budget"] = _centavos(orcamento_diario, moeda)
                else:
                    _checar_total(orcamento_total, inicio, fim, moeda)
                    dados["lifetime_budget"] = _centavos(orcamento_total, moeda)
                dados["bid_strategy"] = estrategia
            if estrategia in ("COST_CAP", "LOWEST_COST_WITH_BID_CAP"):
                if limite_cpa is None:
                    raise MetaError("A estratégia de lance pede limite_cpa.")
                dados["bid_amount"] = _centavos(limite_cpa, moeda)
            if estrategia == "LOWEST_COST_WITH_MIN_ROAS":
                if roas_minimo is None:
                    raise MetaError("A estratégia de lance pede roas_minimo.")
                dados["bid_constraints"] = {"roas_average_floor": int(round(roas_minimo * 10000))}
            dados["targeting"] = alvo
            if inicio:
                dados["start_time"] = inicio
            if fim:
                dados["end_time"] = fim
            dados.update(extras)
            try:
                r = await _post(c, f"{conta['act']}/adsets", dados)
            except MetaError as e:
                _registrar("criar_conjunto", str(campanha_id), params, False, str(e))
                raise
        msg = _ok_pausado("Conjunto", str(r.get("id")), nome)
        _registrar("criar_conjunto", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Campanha: {camp.get('name')} ({campanha_id})\n{msg}"
    except Exception as e:
        return _tool_error(e)


async def _subir_imagem(c: httpx.AsyncClient, act: str, url: Optional[str], arquivo: Optional[str]) -> str:
    import base64
    if arquivo:
        p = Path(arquivo).expanduser()
        if p.suffix.lower() not in _IMAGENS_OK or not p.is_file():
            raise MetaError("imagem_arquivo precisa ser um arquivo .jpg, .jpeg, .png ou .gif existente neste Mac.")
        if p.stat().st_size > 30 * 1024 * 1024:
            raise MetaError("A imagem passa de 30 MB.")
        conteudo, nome = p.read_bytes(), p.name
    else:
        if not re.match(r"^https://", url or ""):
            raise MetaError("imagem_url precisa começar com https://.")
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, transport=_TRANSPORT) as livre:  # sem token
            r = await livre.get(url)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise MetaError(f"Não consegui baixar uma imagem de {url} (HTTP {r.status_code}).")
        conteudo, nome = r.content, (url.rsplit("/", 1)[-1].split("?")[0] or "imagem")[:80]
        if len(conteudo) > 30 * 1024 * 1024:
            raise MetaError("A imagem passa de 30 MB.")
    resp = await _post(c, f"{act}/adimages", {"bytes": base64.b64encode(conteudo).decode(), "name": nome})
    imagens = resp.get("images") or {}
    for v in imagens.values():
        if v.get("hash"):
            return str(v["hash"])
    raise MetaError("A Meta não devolveu o hash da imagem enviada.")


_CARTAO_CAMPOS = {"imagem_hash", "imagem_url", "imagem_arquivo", "video_id", "link", "titulo", "descricao", "cta"}
_CTAS_VALIDOS = set(get_args(_CTAS))
_VIDEOS_OK = {".mp4", ".mov"}
_VIDEO_MAX_BYTES = 4 * 1024 * 1024 * 1024
_MIDIAS_CARTAO = ("imagem_hash", "imagem_url", "imagem_arquivo", "video_id")


async def _info_video(c: httpx.AsyncClient, video_id: str) -> dict:
    v = await _get(c, str(video_id), {"fields": "picture,status"})
    estado = (v.get("status") or {}).get("video_status") or "ready"
    if estado != "ready":
        raise MetaError(f"O vídeo {video_id} ainda não está pronto (status: {estado}). Espere o processamento da Meta e tente de novo.")
    return v


async def _spec_criativo(c: httpx.AsyncClient, conta: dict, *, nome: str, pagina_id: Optional[str], instagram_id: Optional[str],
                         link: Optional[str], texto: Optional[str], titulo: Optional[str], descricao: Optional[str],
                         imagem_hash: Optional[str], imagem_url: Optional[str], imagem_arquivo: Optional[str],
                         video_id: Optional[str], cta: str, parametros_url: Optional[str], cartoes: Optional[list] = None,
                         post_id: Optional[str] = None, post_instagram_id: Optional[str] = None) -> dict:
    """Monta (sem criar) o corpo de um criativo: imagem, vídeo, carrossel, post existente ou post do Instagram."""
    criativo: dict = {"name": f"{nome} - criativo"}
    midia_solta = any((imagem_hash, imagem_url, imagem_arquivo, video_id))
    if post_id:
        if not re.fullmatch(r"\d{5,25}_\d{5,25}", str(post_id)):
            raise MetaError("post_id precisa estar no formato <id da página>_<id do post>.")
        if any((link, texto, titulo, descricao, midia_solta, cartoes, post_instagram_id)):
            raise MetaError("Com post_id o anúncio usa o post como ele está: não combine com texto, link, mídia ou cartões.")
        await _pagina_da_conta(c, conta, str(post_id).split("_")[0])
        criativo["object_story_id"] = str(post_id)
        if instagram_id:
            criativo["instagram_user_id"] = str(instagram_id)
        return criativo
    if post_instagram_id:
        if not re.fullmatch(r"\d{5,25}", str(post_instagram_id)) or not instagram_id:
            raise MetaError("Para impulsionar um post do Instagram informe post_instagram_id (de listar_posts_instagram) e instagram_id.")
        if any((link, texto, titulo, descricao, midia_solta, cartoes)):
            raise MetaError("Com post_instagram_id o anúncio usa o post como ele está: não combine com texto, link, mídia ou cartões.")
        criativo["source_instagram_media_id"] = str(post_instagram_id)
        criativo["instagram_user_id"] = str(instagram_id)
        return criativo
    if not (pagina_id and link and texto):
        raise MetaError("Para montar o criativo informe pagina_id, link e texto (ou use criativo_id).")
    if cta not in _CTAS_VALIDOS:
        raise MetaError(f"cta inválido: {cta}. Opções: {', '.join(sorted(_CTAS_VALIDOS))}.")
    historia: dict = {"page_id": str(pagina_id)}
    if instagram_id:
        historia["instagram_user_id"] = str(instagram_id)
    botao = {"type": cta, "value": {"link": link}}
    if cartoes:
        if midia_solta:
            raise MetaError("Num carrossel a mídia vai dentro de cada cartão: não informe imagem_* nem video_id soltos.")
        if not 2 <= len(cartoes) <= 10:
            raise MetaError("Um carrossel precisa de 2 a 10 cartões.")
        filhos = []
        for k, cart in enumerate(cartoes, 1):
            if not isinstance(cart, dict):
                raise MetaError(f"Cartão {k}: precisa ser um objeto com a mídia, título, descrição e link.")
            fora = sorted(set(cart) - _CARTAO_CAMPOS)
            if fora:
                raise MetaError(f"Cartão {k}: campos desconhecidos {', '.join(fora)}. Use: {', '.join(sorted(_CARTAO_CAMPOS))}.")
            if sum(bool(cart.get(x)) for x in _MIDIAS_CARTAO) != 1:
                raise MetaError(f"Cartão {k}: informe exatamente uma mídia (imagem_hash, imagem_url, imagem_arquivo ou video_id).")
            destino = cart.get("link") or link
            cta_cartao = cart.get("cta") or cta
            if cta_cartao not in _CTAS_VALIDOS:
                raise MetaError(f"Cartão {k}: cta inválido ({cta_cartao}).")
            item: dict = {"link": destino, "call_to_action": {"type": cta_cartao, "value": {"link": destino}}}
            if cart.get("titulo"):
                item["name"] = cart["titulo"]
            if cart.get("descricao"):
                item["description"] = cart["descricao"]
            if cart.get("video_id"):
                item["video_id"] = str(cart["video_id"])
                miniatura = (await _info_video(c, cart["video_id"])).get("picture")
                if miniatura:
                    item["picture"] = miniatura
            elif cart.get("imagem_hash"):
                item["image_hash"] = str(cart["imagem_hash"])
            else:
                item["image_hash"] = await _subir_imagem(c, conta["act"], cart.get("imagem_url"), cart.get("imagem_arquivo"))
            filhos.append(item)
        historia["link_data"] = {"link": link, "message": texto, "child_attachments": filhos, "multi_share_end_card": False}
    else:
        if sum(bool(x) for x in (imagem_hash, imagem_url, imagem_arquivo, video_id)) != 1:
            raise MetaError("Informe exatamente uma mídia: imagem_hash, imagem_url, imagem_arquivo ou video_id.")
        if imagem_url or imagem_arquivo:
            imagem_hash = await _subir_imagem(c, conta["act"], imagem_url, imagem_arquivo)
        if video_id:
            miniatura = (await _info_video(c, video_id)).get("picture")
            historia["video_data"] = {k: v for k, v in {"video_id": str(video_id), "message": texto, "title": titulo,
                                                         "link_description": descricao, "image_url": miniatura,
                                                         "call_to_action": botao}.items() if v}
        else:
            historia["link_data"] = {k: v for k, v in {"link": link, "message": texto, "name": titulo,
                                                        "description": descricao, "image_hash": imagem_hash,
                                                        "call_to_action": botao}.items() if v}
    criativo["object_story_spec"] = historia
    if parametros_url:
        criativo["url_tags"] = parametros_url
    return criativo


@mcp.tool(annotations=_CRIA)
async def criar_anuncio(
    conjunto_id: Annotated[str, Field(description="ID do conjunto onde o anúncio será criado.")],
    nome: Annotated[str, Field(description="Nome do anúncio.", min_length=1, max_length=400)],
    criativo_id: Annotated[Optional[str], Field(description="Usar um criativo que já existe (veja listar_criativos ou criar_criativo). Se informado, os campos abaixo são ignorados.")] = None,
    pagina_id: Annotated[Optional[str], Field(description="Página do Facebook que assina o anúncio (veja listar_paginas).")] = None,
    instagram_id: Annotated[Optional[str], Field(description="Conta do Instagram que assina o anúncio (opcional).")] = None,
    link: Annotated[Optional[str], Field(description="URL de destino.")] = None,
    texto: Annotated[Optional[str], Field(description="Texto principal.")] = None,
    titulo: Annotated[Optional[str], Field(description="Título.")] = None,
    descricao: Annotated[Optional[str], Field(description="Descrição.")] = None,
    imagem_hash: Annotated[Optional[str], Field(description="Imagem que já está na conta (veja listar_midias).")] = None,
    imagem_url: Annotated[Optional[str], Field(description="Imagem pública (https) para enviar à conta.")] = None,
    imagem_arquivo: Annotated[Optional[str], Field(description="Caminho de uma imagem neste Mac para enviar à conta.")] = None,
    video_id: Annotated[Optional[str], Field(description="Vídeo que já está na conta (veja listar_midias ou enviar_video).")] = None,
    cta: Annotated[_CTAS, Field(description="Botão de chamada para ação.")] = "LEARN_MORE",
    parametros_url: Annotated[Optional[str], Field(description="Parâmetros de URL (UTMs), ex.: utm_source=meta&utm_campaign={{campaign.name}}.")] = None,
    cartoes: Annotated[Optional[list[dict]], Field(description="Carrossel: 2 a 10 cartões, cada um com uma mídia (imagem_hash, imagem_url, imagem_arquivo ou video_id) e, opcionais, titulo, descricao, link e cta.")] = None,
    post_id: Annotated[Optional[str], Field(description="Anunciar um post que já existe na página, no formato <id da página>_<id do post>. O post vai como está.")] = None,
    post_instagram_id: Annotated[Optional[str], Field(description="Anunciar um post orgânico do Instagram (id de listar_posts_instagram). Exige instagram_id.")] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Cria um anúncio PAUSADO num conjunto da conta padrão do cliente, com um criativo existente ou montando um novo:
    imagem ou vídeo único, carrossel, post da página ou post do Instagram. Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = dict(conjunto_id=conjunto_id, nome=nome, criativo_id=criativo_id, pagina_id=pagina_id, instagram_id=instagram_id,
                      link=link, texto=texto, titulo=titulo, descricao=descricao, imagem_hash=imagem_hash, imagem_url=imagem_url,
                      imagem_arquivo=imagem_arquivo, video_id=video_id, cta=cta, parametros_url=parametros_url,
                      cartoes=len(cartoes) if cartoes else None, post_id=post_id, post_instagram_id=post_instagram_id)
        async with _client() as c:
            conj, tipo, conta = await _objeto(c, conjunto_id)
            if tipo != "adset":
                raise MetaError(f"{conjunto_id} não é um conjunto de anúncios.")
            try:
                if not criativo_id:
                    criativo = await _spec_criativo(c, conta, nome=nome, pagina_id=pagina_id, instagram_id=instagram_id, link=link,
                                                    texto=texto, titulo=titulo, descricao=descricao, imagem_hash=imagem_hash,
                                                    imagem_url=imagem_url, imagem_arquivo=imagem_arquivo, video_id=video_id, cta=cta,
                                                    parametros_url=parametros_url, cartoes=cartoes, post_id=post_id,
                                                    post_instagram_id=post_instagram_id)
                    criativo_id = str((await _post(c, f"{conta['act']}/adcreatives", criativo)).get("id"))
                r = await _post(c, f"{conta['act']}/ads", {"name": nome, "adset_id": str(conjunto_id),
                                                           "creative": {"creative_id": str(criativo_id)}, "status": "PAUSED"})
            except MetaError as e:
                _registrar("criar_anuncio", str(conjunto_id), params, False, str(e))
                raise
        msg = _ok_pausado("Anúncio", str(r.get("id")), nome) + f" Criativo: {criativo_id}."
        _registrar("criar_anuncio", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conjunto: {conj.get('name')} ({conjunto_id})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_criativo(
    nome: Annotated[str, Field(description="Nome do criativo na biblioteca da conta.", min_length=1, max_length=300)],
    pagina_id: Annotated[Optional[str], Field(description="Página do Facebook que assina (veja listar_paginas). Obrigatória, exceto com post_instagram_id.")] = None,
    instagram_id: Annotated[Optional[str], Field(description="Conta do Instagram que assina (opcional; obrigatória com post_instagram_id).")] = None,
    link: Annotated[Optional[str], Field(description="URL de destino.")] = None,
    texto: Annotated[Optional[str], Field(description="Texto principal.")] = None,
    titulo: Annotated[Optional[str], Field(description="Título.")] = None,
    descricao: Annotated[Optional[str], Field(description="Descrição.")] = None,
    imagem_hash: Annotated[Optional[str], Field(description="Imagem que já está na conta (veja listar_midias).")] = None,
    imagem_url: Annotated[Optional[str], Field(description="Imagem pública (https) para enviar à conta.")] = None,
    imagem_arquivo: Annotated[Optional[str], Field(description="Caminho de uma imagem neste Mac para enviar à conta.")] = None,
    video_id: Annotated[Optional[str], Field(description="Vídeo pronto na conta (veja listar_midias ou enviar_video).")] = None,
    cta: Annotated[_CTAS, Field(description="Botão de chamada para ação.")] = "LEARN_MORE",
    parametros_url: Annotated[Optional[str], Field(description="Parâmetros de URL (UTMs).")] = None,
    cartoes: Annotated[Optional[list[dict]], Field(description="Carrossel: 2 a 10 cartões, cada um com uma mídia e, opcionais, titulo, descricao, link e cta.")] = None,
    post_id: Annotated[Optional[str], Field(description="Usar um post existente da página (<id da página>_<id do post>).")] = None,
    post_instagram_id: Annotated[Optional[str], Field(description="Usar um post orgânico do Instagram (id de listar_posts_instagram).")] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria só o criativo (sem anúncio) na conta padrão do cliente, para reaproveitar em vários anúncios: imagem ou vídeo
    único, carrossel, post da página ou post do Instagram. Exige a permissão 'criar e duplicar'. Use o ID devolvido em
    criar_anuncio (criativo_id)."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = dict(nome=nome, pagina_id=pagina_id, instagram_id=instagram_id, link=link, texto=texto, titulo=titulo,
                      descricao=descricao, imagem_hash=imagem_hash, imagem_url=imagem_url, imagem_arquivo=imagem_arquivo,
                      video_id=video_id, cta=cta, parametros_url=parametros_url, cartoes=len(cartoes) if cartoes else None,
                      post_id=post_id, post_instagram_id=post_instagram_id)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            try:
                criativo = await _spec_criativo(c, conta, nome=nome, pagina_id=pagina_id, instagram_id=instagram_id, link=link,
                                                texto=texto, titulo=titulo, descricao=descricao, imagem_hash=imagem_hash,
                                                imagem_url=imagem_url, imagem_arquivo=imagem_arquivo, video_id=video_id, cta=cta,
                                                parametros_url=parametros_url, cartoes=cartoes, post_id=post_id,
                                                post_instagram_id=post_instagram_id)
                criativo["name"] = nome
                r = await _post(c, f"{conta['act']}/adcreatives", criativo)
            except MetaError as e:
                _registrar("criar_criativo", conta["act"], params, False, str(e))
                raise
        msg = f"OK criativo '{nome}' criado (ID {r.get('id')}). Para anunciar, use criar_anuncio com criativo_id={r.get('id')}."
        _registrar("criar_criativo", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def enviar_video(
    titulo: Annotated[str, Field(description="Título do vídeo na biblioteca da conta.", min_length=1, max_length=200)],
    url: Annotated[Optional[str], Field(description="Endereço https público do arquivo de vídeo (a Meta baixa sozinha).")] = None,
    arquivo: Annotated[Optional[str], Field(description="Caminho completo de um vídeo .mp4 ou .mov neste Mac (até 4 GB; enviado em pedaços).")] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Envia um vídeo para a biblioteca da conta padrão do cliente, a partir de um link https ou de um arquivo neste Mac.
    Exige a permissão 'criar e duplicar'. A Meta processa o vídeo depois do envio: confira com listar_midias (tipo
    videos) até ficar pronto antes de usá-lo em criar_anuncio."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if bool(url) == bool(arquivo):
            raise MetaError("Informe url (https) ou arquivo, só um dos dois.")
        params = {"titulo": titulo, "url": url, "arquivo": Path(arquivo).name if arquivo else None}
        if url and not re.match(r"^https://", url):
            raise MetaError("url precisa começar com https://.")
        p = None
        if arquivo:
            p = Path(arquivo).expanduser()
            if p.suffix.lower() not in _VIDEOS_OK or not p.is_file():
                raise MetaError("arquivo precisa ser um vídeo .mp4 ou .mov existente neste Mac (caminho completo).")
            if p.stat().st_size > _VIDEO_MAX_BYTES:
                raise MetaError("O vídeo passa de 4 GB.")
            if p.stat().st_size == 0:
                raise MetaError("O arquivo de vídeo está vazio.")
            params["tamanho_mb"] = round(p.stat().st_size / 1048576, 1)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            try:
                if url:
                    r = await _post(c, f"{conta['act']}/advideos", {"file_url": url, "title": titulo})
                    video_id = str(r.get("id"))
                else:
                    tamanho = p.stat().st_size
                    ini = await _post(c, f"{conta['act']}/advideos", {"upload_phase": "start", "file_size": tamanho})
                    sessao, video_id = str(ini.get("upload_session_id")), str(ini.get("video_id"))
                    inicio, fim = int(ini.get("start_offset") or 0), int(ini.get("end_offset") or 0)
                    voltas = 0
                    with p.open("rb") as f:
                        while inicio != fim:
                            voltas += 1
                            if voltas > tamanho // 1024 + 10:
                                raise MetaError("A Meta não avançou o envio do vídeo. Nada foi publicado.")
                            f.seek(inicio)
                            pedaco = f.read(fim - inicio)
                            r = await _post(c, f"{conta['act']}/advideos", {"upload_phase": "transfer", "start_offset": inicio,
                                                                           "upload_session_id": sessao},
                                            arquivos={"video_file_chunk": (p.name, pedaco, "application/octet-stream")})
                            inicio, fim = int(r.get("start_offset") or 0), int(r.get("end_offset") or 0)
                    await _post(c, f"{conta['act']}/advideos", {"upload_phase": "finish", "upload_session_id": sessao, "title": titulo})
            except MetaError as e:
                msg = f"O envio do vídeo falhou e nada foi publicado. Detalhe: {e}"
                _registrar("enviar_video", conta["act"], params, False, msg)
                raise MetaError(msg, code=e.code)
        msg = (f"OK vídeo '{titulo}' enviado (ID {video_id}). A Meta ainda vai processar: confira com listar_midias "
               "(tipo videos) até o status ficar pronto antes de usar em criar_anuncio.")
        _registrar("enviar_video", video_id, params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# Leituras de apoio para montar campanhas

@mcp.tool(annotations=_RO)
async def listar_pixels(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Pixels/datasets da conta padrão do cliente (para criar conjuntos de vendas ou leads)."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name,last_fired_time", "limit": 50})
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']}\n" + _csv(
            [{"id": r.get("id"), "nome": r.get("name"), "ultimo_disparo": (r.get("last_fired_time") or "")[:16]} for r in rows],
            ["id", "nome", "ultimo_disparo"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_paginas(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Páginas do Facebook (e Instagram ligado a elas) que a conta padrão do cliente pode usar nos anúncios."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/promote_pages",
                                  {"fields": "id,name,instagram_business_account{id,username}", "limit": 100})
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']}\n" + _csv(
            [{"pagina_id": r.get("id"), "pagina": r.get("name"),
              "instagram_id": (r.get("instagram_business_account") or {}).get("id"),
              "instagram": (r.get("instagram_business_account") or {}).get("username")} for r in rows],
            ["pagina_id", "pagina", "instagram_id", "instagram"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_midias(
    tipo: Annotated[Literal["imagens", "videos"], Field(description="imagens ou videos.")] = "imagens",
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Imagens (com hash) ou vídeos (com ID) já enviados à conta padrão do cliente, mais recentes primeiro."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            if tipo == "imagens":
                rows = await _get(c, f"{conta['act']}/adimages", {"fields": "hash,name,created_time,width,height", "limit": 60})
                linhas = [{"hash": r.get("hash"), "nome": r.get("name"), "tamanho": f"{r.get('width')}x{r.get('height')}",
                           "criada": (r.get("created_time") or "")[:10]} for r in rows.get("data", [])]
                cols = ["hash", "nome", "tamanho", "criada"]
            else:
                rows = await _get(c, f"{conta['act']}/advideos", {"fields": "id,title,length,created_time,status", "limit": 60})
                linhas = [{"video_id": r.get("id"), "titulo": r.get("title"), "segundos": round(_f(r.get("length"))),
                           "criado": (r.get("created_time") or "")[:10],
                           "status": (r.get("status") or {}).get("video_status")} for r in rows.get("data", [])]
                cols = ["video_id", "titulo", "segundos", "criado", "status"]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} {tipo}\n" + _csv(linhas, cols)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def buscar_localizacao(
    termo: Annotated[str, Field(description="Nome da cidade, estado ou país (ex.: 'Feira de Santana').", min_length=2)],
    tipo: Annotated[Literal["cidade", "regiao", "pais"], Field()] = "cidade",
    pais: Annotated[str, Field(description="Código do país para restringir a busca.")] = "BR",
    cliente: _CLIENTE = None,
) -> str:
    """Procura chaves de localização da Meta (cidades, estados, países) para usar em criar_conjunto."""
    try:
        nome_cliente = _usar_cliente(cliente)
        tipos = {"cidade": "city", "regiao": "region", "pais": "country"}
        async with _client() as c:
            r = await _get(c, "search", {"type": "adgeolocation", "q": termo, "location_types": json.dumps([tipos[tipo]]),
                                         "country_code": pais.upper(), "limit": 20})
        linhas = [{"chave": x.get("key"), "nome": x.get("name"), "estado": x.get("region"), "pais": x.get("country_code")}
                  for x in r.get("data", [])]
        return f"Cliente: {nome_cliente} | {len(linhas)} resultado(s) para '{termo}'\n" + _csv(linhas, ["chave", "nome", "estado", "pais"])
    except Exception as e:
        return _tool_error(e)


# ───────────────────────── públicos de lista de clientes ─────────────────────────

_PUBLICO_CHAVES = {"email": "EMAIL", "phone": "PHONE", "fn": "FN", "ln": "LN", "ct": "CT", "st": "ST",
                   "country": "COUNTRY", "zip": "ZIP", "gen": "GEN", "doby": "DOBY", "dobm": "DOBM",
                   "dobd": "DOBD", "madid": "MADID"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOTE_PUBLICO = 10_000  # máximo de linhas por envio à Meta
_LISTA_MAX_BYTES = 500 * 1024 * 1024


def _ler_lista(arquivo: str) -> dict:
    """Lê um CSV de clientes já com hash SHA-256 (colunas email, phone, fn, ln, ct, st, country, doby,
    dobm, dobd e, opcional, value). Descarta linhas sem identificador, repetidas ou fora do formato."""
    p = Path(arquivo).expanduser()
    if p.suffix.lower() != ".csv" or not p.is_file():
        raise MetaError("arquivo_csv precisa ser um arquivo .csv existente neste Mac (caminho completo).")
    if p.stat().st_size > _LISTA_MAX_BYTES:
        raise MetaError("O arquivo passa de 500 MB. Divida a lista em partes.")
    with p.open(newline="", encoding="utf-8-sig") as f:
        leitor = csv.reader(f)
        try:
            cab = [h.strip().lower() for h in next(leitor)]
        except StopIteration:
            raise MetaError("O arquivo está vazio.")
        colunas = [(i, _PUBLICO_CHAVES[h]) for i, h in enumerate(cab) if h in _PUBLICO_CHAVES]
        idx_valor = next((i for i, h in enumerate(cab) if h in ("value", "valor")), None)
        if not any(s in ("EMAIL", "PHONE", "MADID") for _, s in colunas):
            raise MetaError("O CSV precisa de pelo menos uma coluna email, phone ou madid (cabeçalho na primeira linha). "
                            f"Colunas encontradas: {', '.join(cab[:12])}.")
        idx_id = [k for k, (_, s) in enumerate(colunas) if s in ("EMAIL", "PHONE", "MADID")]
        linhas, vistos = [], set()
        sem_id = invalidas = repetidas = 0
        soma = 0.0
        for row in leitor:
            if not row:
                continue
            vals = [(row[i].strip().lower() if i < len(row) else "") for i, _ in colunas]
            if any(v and not _SHA256.fullmatch(v) for v in vals):
                invalidas += 1
                continue
            if not any(vals[k] for k in idx_id):
                sem_id += 1
                continue
            chave = tuple(vals[k] for k in idx_id)
            if chave in vistos:
                repetidas += 1
                continue
            vistos.add(chave)
            if idx_valor is not None:
                bruto = row[idx_valor].strip() if idx_valor < len(row) else ""
                valor = _numero(bruto)
                if valor is None or valor < 0:
                    invalidas += 1
                    continue
                soma += valor
                vals.append(f"{valor:.2f}")
            linhas.append(vals)
    total = len(linhas) + sem_id + invalidas + repetidas
    if invalidas and invalidas > max(5, total * 0.05):
        raise MetaError(f"{invalidas} de {total} linhas têm valores que não são SHA-256 (64 caracteres hexadecimais). "
                        "O arquivo parece não estar com hash. Nada foi enviado.")
    if not linhas:
        raise MetaError("Nenhuma linha utilizável no arquivo (todas sem email/telefone ou fora do formato).")
    schema = [s for _, s in colunas] + (["LOOKALIKE_VALUE"] if idx_valor is not None else [])
    return {"schema": schema, "linhas": linhas, "lidas": total, "sem_id": sem_id, "invalidas": invalidas,
            "repetidas": repetidas, "soma_valor": soma if idx_valor is not None else None,
            "com_valor": idx_valor is not None, "arquivo": p.name}


def _resumo_lista(lista: dict) -> str:
    n = len(lista["linhas"])
    s = lista["schema"]
    cobertura = ", ".join(f"{c} {sum(1 for l in lista['linhas'] if l[k]) :,}" for k, c in enumerate(s) if c in ("EMAIL", "PHONE"))
    txt = (f"Arquivo {lista['arquivo']}: {lista['lidas']:,} linhas lidas, {n:,} utilizáveis"
           f" (descartadas: {lista['sem_id']:,} sem email/telefone, {lista['repetidas']:,} repetidas, {lista['invalidas']:,} fora do formato).\n"
           f"Colunas enviadas: {', '.join(s)}. Preenchimento: {cobertura}.")
    if lista["com_valor"]:
        txt += f"\nValor total da lista: {lista['soma_valor']:,.2f}."
    return txt


@mcp.tool(annotations=_CRIA)
async def criar_publico_lista(
    nome: Annotated[str, Field(description="Nome do público personalizado (lista de clientes).", min_length=1, max_length=200)],
    arquivo_csv: Annotated[str, Field(description="Caminho completo do CSV neste Mac. Colunas já com hash SHA-256: email, phone, fn, ln, ct, st, country, doby, dobm, dobd e, opcional, value.")],
    descricao: Annotated[Optional[str], Field(description="Descrição do público.", max_length=500)] = None,
    baseado_em_valor: Annotated[Optional[bool], Field(description="Público baseado em valor (usa a coluna value). Vazio: ligado se o CSV tem coluna value ou valor.")] = None,
    simular: Annotated[bool, Field(description="True: só confere o arquivo e mostra o que seria enviado, sem criar nada na Meta.")] = False,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria um público personalizado de lista de clientes na conta padrão do cliente e envia o CSV em lotes de
    10.000 linhas. Exige a permissão 'criar e duplicar'. O arquivo precisa estar com hash SHA-256 (nada em texto
    puro sai do Mac). Se já existir público com o mesmo nome, recusa. Use simular=True antes para conferir."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        lista = await asyncio.to_thread(_ler_lista, arquivo_csv)
        usar_valor = lista["com_valor"] if baseado_em_valor is None else baseado_em_valor
        if usar_valor and not lista["com_valor"]:
            raise MetaError("baseado_em_valor exige uma coluna value (ou valor) no CSV.")
        if not usar_valor and lista["com_valor"]:
            lista["schema"] = lista["schema"][:-1]
            lista["linhas"] = [l[:-1] for l in lista["linhas"]]
        linhas, schema = lista["linhas"], lista["schema"]
        lotes = (len(linhas) + _LOTE_PUBLICO - 1) // _LOTE_PUBLICO
        resumo = _resumo_lista(lista)
        params = {"nome": nome, "arquivo": lista["arquivo"], "linhas": len(linhas), "lotes": lotes,
                  "baseado_em_valor": usar_valor, "descricao": descricao}
        if simular:
            return f"SIMULAÇÃO, nada foi enviado à Meta. Cliente: {nome_cliente}\n{resumo}\nSeriam {lotes} lote(s) de até {_LOTE_PUBLICO:,} linhas."
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            existentes = await _get_all(c, f"{conta['act']}/customaudiences", {"fields": "id,name", "limit": 200})
            igual = next((a for a in existentes if str(a.get("name", "")).strip().lower() == nome.strip().lower()), None)
            if igual:
                raise MetaError(f"Já existe um público chamado '{igual.get('name')}' ({igual.get('id')}) nesta conta. "
                                "Nada foi criado nem enviado. Escolha outro nome.")
            try:
                r = await _post(c, f"{conta['act']}/customaudiences",
                                {"name": nome, "subtype": "CUSTOM", "description": descricao,
                                 "customer_file_source": "USER_PROVIDED_ONLY",
                                 "is_value_based": True if usar_valor else None})
            except MetaError as e:
                _registrar("criar_publico_lista", conta["act"], params, False, str(e))
                if "terms" in str(e).lower() or "termos" in str(e).lower():
                    raise MetaError(f"{e} É preciso aceitar os Termos de Públicos Personalizados da conta no Gerenciador de Anúncios.", code=e.code)
                raise
            aud = str(r.get("id"))
            sessao = int.from_bytes(os.urandom(6), "big")
            recebidas = invalidas_meta = 0
            for k in range(lotes):
                parte = linhas[k * _LOTE_PUBLICO:(k + 1) * _LOTE_PUBLICO]
                try:
                    resp = await _post(c, f"{aud}/users", {
                        "payload": {"schema": schema, "data": parte},
                        "session": {"session_id": sessao, "batch_seq": k + 1, "last_batch_flag": k == lotes - 1,
                                    "estimated_num_total": len(linhas)}})
                except MetaError as e:
                    msg = (f"O público '{nome}' ({aud}) foi criado, mas o envio parou no lote {k + 1} de {lotes}. "
                           f"A Meta aceitou {recebidas:,} linhas antes disso. Não repeti nada: confira com listar_publicos "
                           f"e me diga se quer completar. Detalhe: {e}")
                    _registrar("criar_publico_lista", aud, params, False, msg)
                    raise MetaError(msg, code=e.code)
                recebidas += int(resp.get("num_received") or len(parte))
                invalidas_meta += int(resp.get("num_invalid_entries") or 0)
        msg = (f"OK público '{nome}' ({aud}) criado e {recebidas:,} linhas enviadas em {lotes} lote(s); "
               f"{invalidas_meta:,} entradas inválidas segundo a Meta. Ele leva alguns minutos para processar e mostrar o tamanho: "
               "confira com listar_publicos.")
        _registrar("criar_publico_lista", aud, params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{resumo}\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_publicos(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Públicos personalizados da conta padrão do cliente, com tamanho aproximado e status (para conferir um upload de lista)."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/customaudiences", {
                "fields": "id,name,subtype,approximate_count_lower_bound,approximate_count_upper_bound,operation_status,delivery_status,time_created",
                "limit": 100})
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "tipo": r.get("subtype"),
                   "tamanho_min": r.get("approximate_count_lower_bound"), "tamanho_max": r.get("approximate_count_upper_bound"),
                   "processamento": (r.get("operation_status") or {}).get("description"),
                   "entrega": (r.get("delivery_status") or {}).get("description"),
                   "criado": (r.get("time_created") or "")[:16]} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} público(s)\n" + _csv(
            linhas, ["id", "nome", "tipo", "tamanho_min", "tamanho_max", "processamento", "entrega", "criado"])
    except Exception as e:
        return _tool_error(e)



# ───────────────────────── públicos: site, engajamento, semelhantes, edição ─────────────────────────

_NOME_EVENTO = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,49}$")
_EVENTOS_ENGAJAMENTO = {
    "instagram": {"ig_business_profile_all": "todos que interagiram com o perfil", "ig_business_profile_engaged": "engajaram com post ou anúncio",
                  "ig_user_messaged_business": "enviaram mensagem no Direct", "INSTAGRAM_PROFILE_FOLLOW": "seguem o perfil hoje"},
    "pagina": {"page_engaged": "interagiram com a página", "page_visited": "visitaram a página", "page_liked": "curtem a página hoje",
               "page_messaged": "enviaram mensagem à página", "page_cta_clicked": "clicaram no botão da página",
               "page_or_post_save": "salvaram a página ou um post"},
    "formulario": {"lead_generation_opened": "abriram o formulário", "lead_generation_submitted": "enviaram o formulário",
                   "lead_generation_dropoff": "abriram e não enviaram"},
}
_FONTE_ENGAJAMENTO = {"instagram": "ig_business", "pagina": "page", "formulario": "lead"}
_SEM_RETENCAO = {"INSTAGRAM_PROFILE_FOLLOW", "page_liked"}  # eventos de "hoje": retenção 0


async def _nomes_livres(c: httpx.AsyncClient, conta: dict, nomes: list) -> None:
    existentes = await _get_all(c, f"{conta['act']}/customaudiences", {"fields": "id,name", "limit": 200})
    usados = {str(a.get("name", "")).strip().lower(): a for a in existentes}
    for n in nomes:
        igual = usados.get(n.strip().lower())
        if igual:
            raise MetaError(f"Já existe um público chamado '{igual.get('name')}' ({igual.get('id')}) nesta conta. "
                            "Nada foi criado. Escolha outro nome.")


def _evento_valido(valor: str, campo: str = "evento") -> str:
    if not _NOME_EVENTO.match(valor or ""):
        raise MetaError(f"{campo} inválido: {valor!r}. Use o nome do evento do pixel (ex.: Purchase, Lead, ViewContent).")
    return valor


def _regra_site(pixel_id: str, tipo: str, url_contem: Optional[str], evento: Optional[str], percentil: int, dias: int,
                excluir_evento: Optional[str], excluir_url: Optional[str]) -> dict:
    fonte = [{"type": "pixel", "id": str(pixel_id)}]
    seg = dias * 86400
    if tipo == "todos_visitantes":
        filtros = [{"field": "url", "operator": "i_contains", "value": ""}]
        template = "ALL_VISITORS"
    elif tipo == "urls":
        if not url_contem:
            raise MetaError("tipo 'urls' exige url_contem (trecho do endereço, ex.: /checkout).")
        filtros = [{"field": "url", "operator": "i_contains", "value": url_contem}]
        template = "VISITORS_BY_URL"
    elif tipo == "evento":
        if not evento:
            raise MetaError("tipo 'evento' exige evento (ex.: AddToCart, InitiateCheckout, Lead, Purchase).")
        filtros = [{"field": "event", "operator": "eq", "value": _evento_valido(evento)}]
        if url_contem:
            filtros.append({"field": "url", "operator": "i_contains", "value": url_contem})
        template = "VISITORS_BY_URL"
    else:  # tempo_no_site
        filtros = [{"field": "url", "operator": "i_contains", "value": ""}]
        template = "TOP_TIME_SPENDERS"
    regra: dict = {"event_sources": fonte, "retention_seconds": seg, "template": template,
                   "filter": {"operator": "and", "filters": filtros}}
    if tipo == "tempo_no_site":
        regra["aggregation"] = {"type": "time_spent", "method": "percentile", "operator": "in_range",
                                "value": {"from": 100 - percentil, "to": 100}}
    rule: dict = {"inclusions": {"operator": "or", "rules": [regra]}}
    filtros_ex = []
    if excluir_evento:
        filtros_ex.append({"field": "event", "operator": "eq", "value": _evento_valido(excluir_evento, "excluir_evento")})
    if excluir_url:
        filtros_ex.append({"field": "url", "operator": "i_contains", "value": excluir_url})
    if filtros_ex:
        rule["exclusions"] = {"operator": "or", "rules": [
            {"event_sources": fonte, "retention_seconds": seg, "template": "VISITORS_BY_URL",
             "filter": {"operator": "or", "filters": filtros_ex}}]}
    return rule


@mcp.tool(annotations=_CRIA)
async def criar_publico_site(
    nome: Annotated[str, Field(description="Nome do público.", min_length=1, max_length=200)],
    tipo: Annotated[Literal["todos_visitantes", "urls", "evento", "tempo_no_site"], Field(
        description="todos_visitantes; urls (visitou páginas com certo trecho no endereço); evento (disparou um evento do pixel, "
                    "ex.: AddToCart); tempo_no_site (os que mais ficaram no site).")] = "todos_visitantes",
    pixel_id: Annotated[Optional[str], Field(description="Pixel (veja listar_pixels). Vazio: usa o único pixel da conta.")] = None,
    url_contem: Annotated[Optional[str], Field(description="Trecho do endereço (tipo urls, ou para refinar o tipo evento).")] = None,
    evento: Annotated[Optional[str], Field(description="Evento do pixel (tipo evento): ViewContent, AddToCart, InitiateCheckout, Purchase, Lead, CompleteRegistration ou um evento personalizado.")] = None,
    percentil_tempo: Annotated[Literal[5, 10, 25], Field(description="Tipo tempo_no_site: os X% que mais ficaram.")] = 25,
    retencao_dias: Annotated[int, Field(description="Quantos dias a pessoa fica no público depois do evento (1 a 180).", ge=1, le=180)] = 30,
    excluir_evento: Annotated[Optional[str], Field(description="Tira do público quem disparou este evento no mesmo período (ex.: Purchase, para remarketing de quem não comprou).")] = None,
    excluir_url_contem: Annotated[Optional[str], Field(description="Tira do público quem visitou páginas com este trecho (ex.: /obrigado).")] = None,
    descricao: Annotated[Optional[str], Field(max_length=500)] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria um público personalizado de visitantes do site (pixel) na conta padrão do cliente: todos os visitantes, quem
    visitou certas páginas, quem disparou um evento ou os que mais ficaram no site, com exclusões opcionais. Exige a
    permissão 'criar e duplicar'. Ele se preenche sozinho a partir dos eventos já registrados pelo pixel."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = dict(nome=nome, tipo=tipo, pixel_id=pixel_id, url_contem=url_contem, evento=evento,
                      percentil_tempo=percentil_tempo if tipo == "tempo_no_site" else None, retencao_dias=retencao_dias,
                      excluir_evento=excluir_evento, excluir_url_contem=excluir_url_contem, descricao=descricao)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
            if not pixel_id:
                if len(pixels) != 1:
                    raise MetaError("Informe o pixel_id. Pixels da conta: " + ("; ".join(f"{p.get('name')} ({p.get('id')})" for p in pixels) or "nenhum"))
                pixel_id = str(pixels[0]["id"])
            elif str(pixel_id) not in {str(p.get("id")) for p in pixels}:
                raise MetaError(f"O pixel {pixel_id} não é da conta padrão do cliente. Pixels da conta: "
                                + ("; ".join(f"{p.get('name')} ({p.get('id')})" for p in pixels) or "nenhum") + ".")
            rule = _regra_site(str(pixel_id), tipo, url_contem, evento, percentil_tempo, retencao_dias, excluir_evento, excluir_url_contem)
            await _nomes_livres(c, conta, [nome])
            try:
                r = await _post(c, f"{conta['act']}/customaudiences", {"name": nome, "subtype": "WEBSITE", "description": descricao,
                                                                         "rule": rule, "prefill": True})
            except MetaError as e:
                _registrar("criar_publico_site", conta["act"], params, False, str(e))
                raise
        msg = (f"OK público de site '{nome}' ({r.get('id')}) criado ({tipo}, {retencao_dias} dias). Ele se preenche com os eventos "
               "do pixel e leva alguns minutos para mostrar o tamanho: confira com listar_publicos.")
        _registrar("criar_publico_site", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_publico_engajamento(
    nome: Annotated[str, Field(description="Nome do público.", min_length=1, max_length=200)],
    origem: Annotated[Literal["instagram", "pagina", "formulario"], Field(description="De onde vem o engajamento.")],
    origem_id: Annotated[str, Field(description="ID do perfil do Instagram (instagram_id de listar_paginas), da página do Facebook ou do formulário instantâneo.")],
    evento: Annotated[str, Field(description="Evento. Instagram: ig_business_profile_all, ig_business_profile_engaged, ig_user_messaged_business, "
                                             "INSTAGRAM_PROFILE_FOLLOW. Página: page_engaged, page_visited, page_liked, page_messaged, "
                                             "page_cta_clicked, page_or_post_save. Formulário: lead_generation_opened, lead_generation_submitted, "
                                             "lead_generation_dropoff.")],
    retencao_dias: Annotated[int, Field(description="Dias de retenção (1 a 365). Ignorado nos eventos de 'hoje' (seguem/curtem).", ge=1, le=365)] = 30,
    pagina_id: Annotated[Optional[str], Field(description="Página dona do formulário (origem formulario).")] = None,
    descricao: Annotated[Optional[str], Field(max_length=500)] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria um público personalizado de engajamento (Instagram, página do Facebook ou formulário instantâneo) na conta
    padrão do cliente. Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if evento not in _EVENTOS_ENGAJAMENTO[origem]:
            raise MetaError(f"Evento '{evento}' não vale para {origem}. Opções: "
                            + "; ".join(f"{k} ({v})" for k, v in _EVENTOS_ENGAJAMENTO[origem].items()) + ".")
        if not re.fullmatch(r"\d{5,25}", str(origem_id)):
            raise MetaError("origem_id precisa ser um ID numérico.")
        params = dict(nome=nome, origem=origem, origem_id=origem_id, evento=evento, retencao_dias=retencao_dias,
                      pagina_id=pagina_id, descricao=descricao)
        fonte = {"type": _FONTE_ENGAJAMENTO[origem], "id": str(origem_id)}
        if origem == "formulario" and pagina_id:
            fonte["owner_id"] = str(pagina_id)
        rule = {"inclusions": {"operator": "or", "rules": [{
            "event_sources": [fonte], "retention_seconds": 0 if evento in _SEM_RETENCAO else retencao_dias * 86400,
            "filter": {"operator": "or", "filters": [{"field": "event", "operator": "eq", "value": evento}]}}]}}
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            await _nomes_livres(c, conta, [nome])
            try:
                r = await _post(c, f"{conta['act']}/customaudiences", {"name": nome, "subtype": "ENGAGEMENT", "description": descricao,
                                                                         "rule": rule, "prefill": True})
            except MetaError as e:
                _registrar("criar_publico_engajamento", conta["act"], params, False, str(e))
                raise
        msg = (f"OK público de engajamento '{nome}' ({r.get('id')}) criado ({origem}: {_EVENTOS_ENGAJAMENTO[origem][evento]}). "
               "Confira o tamanho com listar_publicos em alguns minutos.")
        _registrar("criar_publico_engajamento", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_publico_semelhante(
    origem_id: Annotated[str, Field(description="ID do público de origem (lista de clientes, site ou engajamento) da conta. Semelhante de semelhante não existe.")],
    percentuais: Annotated[list[float], Field(
        description="Percentuais de semelhança, de 1 a 20 (1 = os 1% mais parecidos). Cada um vira um público. Ex.: [1, 3, 5].",
        min_length=1, max_length=10)] = [1.0],
    paises: Annotated[list[str], Field(description="Países onde procurar pessoas parecidas (ex.: ['BR']).", min_length=1)] = ["BR"],
    nome: Annotated[Optional[str], Field(description="Nome base. Vazio: 'Semelhante 1% (BR) - <origem>'. Com vários percentuais o % entra no nome.", max_length=180)] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria públicos semelhantes (lookalike) a partir de um público da conta padrão do cliente, um por percentual pedido.
    Exige a permissão 'criar e duplicar'. A origem precisa ter ao menos 100 pessoas do país escolhido; cada semelhante
    leva de alguns minutos a algumas horas para ficar pronto."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        percs = sorted({round(float(p), 2) for p in percentuais})
        if any(p < 1 or p > 20 for p in percs):
            raise MetaError("Os percentuais precisam estar entre 1 e 20.")
        codigos = [re.sub(r"[^A-Za-z]", "", p).upper() for p in paises]
        if any(len(p) != 2 for p in codigos):
            raise MetaError("paises precisa de códigos de 2 letras (ex.: BR).")
        params = dict(origem_id=origem_id, percentuais=percs, paises=codigos, nome=nome)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            origem = (await _publicos_da_conta(c, conta, [origem_id]))[0]
            if str(origem.get("subtype")).upper() == "LOOKALIKE":
                raise MetaError("A origem já é um público semelhante. Use um público de lista, de site ou de engajamento.")
            pais_txt = "/".join(codigos)
            nomes = [(nome + (f" {int(p) if p == int(p) else p}%" if len(percs) > 1 else "")) if nome
                     else f"Semelhante {int(p) if p == int(p) else p}% ({pais_txt}) - {origem.get('name')}" for p in percs]
            await _nomes_livres(c, conta, nomes)
            criados, falha = [], None
            for p, n in zip(percs, nomes):
                spec = {"ratio": round(p / 100, 4)}
                if len(codigos) == 1:
                    spec["country"] = codigos[0]
                else:
                    spec["location_spec"] = {"geo_locations": {"countries": codigos}}
                try:
                    r = await _post(c, f"{conta['act']}/customaudiences", {"name": n, "subtype": "LOOKALIKE",
                                                                             "origin_audience_id": str(origem_id), "lookalike_spec": spec})
                    criados.append((n, str(r.get("id"))))
                except MetaError as e:
                    falha = e
                    break
        if falha:
            msg = ((f"Criados antes da falha: {', '.join(f'{n} ({i})' for n, i in criados)}. " if criados else "Nenhum foi criado. ")
                   + f"Não repeti nada. Detalhe: {falha}")
            _registrar("criar_publico_semelhante", str(origem_id), params, False, msg)
            raise MetaError(msg, code=falha.code)
        msg = ("OK " + "; ".join(f"'{n}' ({i})" for n, i in criados)
               + f" criado(s) a partir de '{origem.get('name')}'. Eles ficam prontos em minutos a horas: confira com listar_publicos.")
        _registrar("criar_publico_semelhante", str(origem_id), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def alterar_publico(
    id: Annotated[str, Field(description="ID do público da conta.")],
    nome: Annotated[Optional[str], Field(description="Nome novo.", min_length=1, max_length=200)] = None,
    descricao: Annotated[Optional[str], Field(description="Descrição nova.", max_length=500)] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Muda o nome ou a descrição de um público da conta padrão do cliente. Exige a permissão 'editar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("editar")
        if nome is None and descricao is None:
            raise MetaError("Informe nome ou descricao para alterar.")
        params = {"nome": nome, "descricao": descricao}
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            antigo = (await _publicos_da_conta(c, conta, [id]))[0]
            if nome is not None and nome.strip().lower() != str(antigo.get("name", "")).strip().lower():
                await _nomes_livres(c, conta, [nome])
            try:
                await _post(c, str(id), {"name": nome, "description": descricao})
            except MetaError as e:
                _registrar("alterar_publico", str(id), params, False, str(e))
                raise
        msg = f"público {id}: " + "; ".join(x for x in (f"nome '{antigo.get('name')}' -> '{nome}'" if nome is not None else "",
                                                          "descrição atualizada" if descricao is not None else "") if x)
        _registrar("alterar_publico", str(id), params, True, msg)
        return f"Cliente: {nome_cliente}\nOK {msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def atualizar_publico_lista(
    publico_id: Annotated[str, Field(description="ID de um público de lista de clientes da conta (veja listar_publicos).")],
    arquivo_csv: Annotated[str, Field(description="Caminho completo do CSV neste Mac, no mesmo formato de criar_publico_lista (colunas já com hash SHA-256).")],
    operacao: Annotated[Literal["adicionar", "remover"], Field(description="adicionar linhas ao público ou remover as do arquivo.")] = "adicionar",
    simular: Annotated[bool, Field(description="True: só confere o arquivo, sem enviar nada.")] = False,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Adiciona ou remove pessoas de um público de lista de clientes que já existe na conta padrão do cliente, enviando um
    CSV com hash em lotes de 10.000. Exige a permissão 'editar'. A Meta pode recusar a remoção se o público ficar pequeno
    demais para campanhas ativas."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("editar")
        lista = await asyncio.to_thread(_ler_lista, arquivo_csv)
        if lista["com_valor"] and operacao == "remover":
            lista["schema"] = lista["schema"][:-1]
            lista["linhas"] = [l[:-1] for l in lista["linhas"]]
        linhas, schema = lista["linhas"], lista["schema"]
        lotes = (len(linhas) + _LOTE_PUBLICO - 1) // _LOTE_PUBLICO
        resumo = _resumo_lista(lista)
        params = {"publico_id": publico_id, "arquivo": lista["arquivo"], "linhas": len(linhas), "lotes": lotes, "operacao": operacao}
        if simular:
            return f"SIMULAÇÃO, nada foi enviado à Meta. Cliente: {nome_cliente}\n{resumo}\nSeriam {lotes} lote(s) para {operacao}."
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            aud = (await _publicos_da_conta(c, conta, [publico_id]))[0]
            if str(aud.get("subtype")).upper() != "CUSTOM":
                raise MetaError(f"O público '{aud.get('name')}' não é de lista de clientes (é {aud.get('subtype')}); só esses aceitam linhas por arquivo.")
            sessao = int.from_bytes(os.urandom(6), "big")
            aceitas = invalidas = 0
            for k in range(lotes):
                parte = linhas[k * _LOTE_PUBLICO:(k + 1) * _LOTE_PUBLICO]
                try:
                    resp = await _post(c, f"{publico_id}/users", {
                        "payload": {"schema": schema, "data": parte},
                        "session": {"session_id": sessao, "batch_seq": k + 1, "last_batch_flag": k == lotes - 1,
                                    "estimated_num_total": len(linhas)}}, metodo="POST" if operacao == "adicionar" else "DELETE")
                except MetaError as e:
                    msg = (f"O envio parou no lote {k + 1} de {lotes}; a Meta aceitou {aceitas:,} linhas antes. Não repeti nada: "
                           f"confira com listar_publicos e me diga se quer completar. Detalhe: {e}")
                    _registrar("atualizar_publico_lista", str(publico_id), params, False, msg)
                    raise MetaError(msg, code=e.code)
                aceitas += int(resp.get("num_received") or len(parte))
                invalidas += int(resp.get("num_invalid_entries") or 0)
        verbo = "adicionadas" if operacao == "adicionar" else "removidas"
        msg = f"OK público '{aud.get('name')}' ({publico_id}): {aceitas:,} linhas {verbo} em {lotes} lote(s); {invalidas:,} inválidas segundo a Meta."
        _registrar("atualizar_publico_lista", str(publico_id), params, True, msg)
        return f"Cliente: {nome_cliente}\n{resumo}\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ───────────────────────── pesquisa de público ─────────────────────────

def _sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(ch)).lower()


@mcp.tool(annotations=_RO)
async def buscar_interesses(
    termo: Annotated[str, Field(description="Assunto a procurar (ex.: 'sorteio', 'carros', 'moto').", min_length=2)],
    modo: Annotated[Literal["buscar", "sugerir"], Field(description="buscar: interesses com esse nome; sugerir: interesses parecidos com o termo.")] = "buscar",
    idioma: Annotated[str, Field(description="Idioma dos nomes (ex.: pt_BR, es_LA).")] = "pt_BR",
    limite: Annotated[int, Field(ge=1, le=50)] = 25,
    cliente: _CLIENTE = None,
) -> str:
    """Procura interesses da Meta (com ID e tamanho aproximado do público) para usar em criar_conjunto e atualizar_conjunto.
    Nunca invente IDs de interesse: use sempre os que esta ferramenta devolver."""
    try:
        nome_cliente = _usar_cliente(cliente)
        params = {"limit": limite, "locale": idioma}
        if modo == "sugerir":
            params.update(type="adinterestsuggestion", interest_list=json.dumps([termo], ensure_ascii=False))
        else:
            params.update(type="adinterest", q=termo)
        async with _client() as c:
            r = await _get(c, "search", params)
        linhas = [{"id": x.get("id"), "nome": x.get("name"), "tamanho_min": x.get("audience_size_lower_bound"),
                   "tamanho_max": x.get("audience_size_upper_bound"), "caminho": " > ".join(x.get("path") or [])}
                  for x in r.get("data", [])]
        return f"Cliente: {nome_cliente} | {len(linhas)} interesse(s) para '{termo}'\n" + _csv(
            linhas, ["id", "nome", "tamanho_min", "tamanho_max", "caminho"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def estimar_publico(
    paises: Annotated[list[str], Field(description="Códigos de país. Ignorado se houver regiões ou cidades.")] = ["BR"],
    regioes: Annotated[list[str], Field(description="Chaves de estados/regiões (buscar_localizacao).")] = [],
    cidades: Annotated[list[str], Field(description="Chaves de cidades (buscar_localizacao).")] = [],
    raio_km: Annotated[Optional[int], Field(ge=10, le=80)] = None,
    idade_min: Annotated[int, Field(ge=18, le=65)] = 18,
    idade_max: Annotated[int, Field(ge=18, le=65)] = 65,
    genero: Annotated[Literal["todos", "homens", "mulheres"], Field()] = "todos",
    publicos_incluir: Annotated[list[str], Field(description="IDs de públicos da conta para incluir.")] = [],
    publicos_excluir: Annotated[list[str], Field(description="IDs de públicos da conta para excluir.")] = [],
    interesses: Annotated[list[str], Field(description="IDs de interesses (buscar_interesses).")] = [],
    posicionamentos: Annotated[list[_POSICAO], Field(description="Posicionamentos manuais. Vazio: automático.")] = [],
    meta: Annotated[Literal["REACH", "LINK_CLICKS", "IMPRESSIONS", "LANDING_PAGE_VIEWS"], Field(description="Meta de otimização da estimativa.")] = "REACH",
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Estima o tamanho do público de uma segmentação na conta padrão do cliente (pessoas por mês e por dia) antes de
    criar o conjunto. Só leitura."""
    try:
        nome_cliente = _usar_cliente(cliente)
        alvo = _montar_alvo(paises=paises, regioes=regioes, cidades=cidades, raio_km=raio_km, idade_min=idade_min,
                            idade_max=idade_max, genero=genero, publico_advantage=False, incluir=publicos_incluir,
                            excluir=publicos_excluir, interesses=interesses, posicoes=posicionamentos)
        alvo.pop("targeting_automation", None)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            r = await _get(c, f"{conta['act']}/delivery_estimate", {"optimization_goal": meta,
                                                                   "targeting_spec": json.dumps(alvo, ensure_ascii=False)})
        e = (r.get("data") or [{}])[0]
        if not e:
            raise MetaError("A Meta não devolveu estimativa para essa segmentação.")
        pronto = e.get("estimate_ready")
        mau = (f"{int(e.get('estimate_mau_lower_bound') or 0):,} a {int(e.get('estimate_mau_upper_bound') or 0):,} pessoas por mês"
               if e.get("estimate_mau_lower_bound") is not None else "sem estimativa mensal")
        dau = f"{int(e['estimate_dau']):,} pessoas por dia" if e.get("estimate_dau") is not None else "sem estimativa diária"
        return (f"Cliente: {nome_cliente} | Conta: {conta['nome']}\nPúblico estimado: {mau}; {dau}"
                + ("" if pronto in (None, True) else " (estimativa ainda sendo calculada)") + ".")
    except Exception as e:
        return _tool_error(e)


# ───────────────────────── editar conjunto e campanha que já existem ─────────────────────────

@mcp.tool(annotations=_MUDA)
async def atualizar_conjunto(
    id: Annotated[str, Field(description="ID do conjunto de anúncios.")],
    publicos_incluir: Annotated[Optional[list[str]], Field(description="IDs de públicos da conta a incluir. Substitui a lista atual; lista vazia limpa. Omitido: não mexe.")] = None,
    publicos_excluir: Annotated[Optional[list[str]], Field(description="IDs de públicos da conta a excluir. Substitui a lista atual; lista vazia limpa.")] = None,
    interesses: Annotated[Optional[list[str]], Field(description="IDs de interesses (buscar_interesses). Substitui os atuais; lista vazia limpa.")] = None,
    paises: Annotated[Optional[list[str]], Field(description="Substitui a localização por estes países.")] = None,
    regioes: Annotated[Optional[list[str]], Field(description="Substitui a localização por estas regiões (buscar_localizacao).")] = None,
    cidades: Annotated[Optional[list[str]], Field(description="Substitui a localização por estas cidades (buscar_localizacao).")] = None,
    raio_km: Annotated[Optional[int], Field(ge=10, le=80)] = None,
    idade_min: Annotated[Optional[int], Field(ge=18, le=65)] = None,
    idade_max: Annotated[Optional[int], Field(ge=18, le=65)] = None,
    genero: Annotated[Optional[Literal["todos", "homens", "mulheres"]], Field()] = None,
    posicionamentos: Annotated[Optional[list[_POSICAO]], Field(description="Posicionamentos manuais. Lista vazia volta para o automático (Advantage+).")] = None,
    publico_advantage: Annotated[Optional[bool], Field(description="Liga ou desliga o público Advantage+.")] = None,
    data_inicio: Annotated[Optional[str], Field(description="Novo início (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")] = None,
    data_fim: Annotated[Optional[str], Field(description="Novo fim (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")] = None,
    campos_extras: Annotated[Optional[str], Field(description="JSON com campos avançados permitidos: " + ", ".join(sorted(_EXTRAS_CONJUNTO)))] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Muda a configuração de um conjunto de anúncios da conta padrão do cliente: públicos incluídos e excluídos,
    interesses, localização, idade, gênero, posicionamentos, datas e campos avançados. Só altera o que for informado e
    mantém o resto da segmentação. Exige a permissão 'editar'. Orçamento e lance continuam em alterar_orcamento."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("editar")
        informado = [publicos_incluir, publicos_excluir, interesses, paises, regioes, cidades, idade_min, idade_max, genero,
                     posicionamentos, publico_advantage, data_inicio, data_fim, campos_extras]
        if all(v is None for v in informado):
            raise MetaError("Informe ao menos uma coisa para alterar.")
        inicio = _data_meta(data_inicio, "data_inicio") if data_inicio else None
        fim = _data_meta(data_fim, "data_fim", fim=True) if data_fim else None
        extras = _extras(campos_extras, _EXTRAS_CONJUNTO)
        params = {k: v for k, v in dict(publicos_incluir=publicos_incluir, publicos_excluir=publicos_excluir, interesses=interesses,
                  paises=paises, regioes=regioes, cidades=cidades, raio_km=raio_km, idade_min=idade_min, idade_max=idade_max,
                  genero=genero, posicionamentos=posicionamentos, publico_advantage=publico_advantage, data_inicio=data_inicio,
                  data_fim=data_fim, campos_extras=campos_extras).items() if v is not None}
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, id, "targeting,start_time,end_time")
            if tipo != "adset":
                raise MetaError(f"Este ID é {_tipo_frase(tipo)}; atualizar_conjunto só vale para conjuntos.")
            alvo = json.loads(json.dumps(obj.get("targeting") or {}))
            alvo.pop("age_range", None)
            mudou = []
            if any(v is not None for v in (paises, regioes, cidades)):
                alvo["geo_locations"] = _geo(paises or [], regioes or [], cidades or [], raio_km)
                mudou.append("localização")
            if idade_min is not None or idade_max is not None:
                novo_min = idade_min if idade_min is not None else int(alvo.get("age_min") or 18)
                novo_max = idade_max if idade_max is not None else int(alvo.get("age_max") or 65)
                if novo_min > novo_max:
                    raise MetaError("A idade mínima não pode ser maior que a máxima.")
                alvo["age_min"], alvo["age_max"] = novo_min, novo_max
                mudou.append(f"idade {novo_min}-{novo_max}")
            if genero is not None:
                if genero == "todos":
                    alvo.pop("genders", None)
                else:
                    alvo["genders"] = [1] if genero == "homens" else [2]
                mudou.append(f"gênero {genero}")
            if publicos_incluir is not None:
                await _publicos_da_conta(c, conta, publicos_incluir)
                alvo["custom_audiences"] = [{"id": str(i)} for i in publicos_incluir]
                if not publicos_incluir:
                    alvo.pop("custom_audiences")
                mudou.append(f"públicos incluídos: {len(publicos_incluir)}")
            if publicos_excluir is not None:
                await _publicos_da_conta(c, conta, publicos_excluir)
                alvo["excluded_custom_audiences"] = [{"id": str(i)} for i in publicos_excluir]
                if not publicos_excluir:
                    alvo.pop("excluded_custom_audiences")
                mudou.append(f"públicos excluídos: {len(publicos_excluir)}")
            if interesses is not None:
                if interesses:
                    alvo["flexible_spec"] = [{"interests": [{"id": _id_interesse(i)} for i in interesses]}]
                else:
                    alvo.pop("flexible_spec", None)
                mudou.append(f"interesses: {len(interesses)}")
            if posicionamentos is not None:
                for k in [k for k in alvo if k == "publisher_platforms" or k.endswith("_positions")]:
                    alvo.pop(k)
                if posicionamentos:
                    _aplicar_posicoes(alvo, posicionamentos)
                mudou.append("posicionamentos manuais" if posicionamentos else "posicionamentos automáticos")
            if publico_advantage is not None:
                alvo["targeting_automation"] = dict(alvo.get("targeting_automation") or {}, advantage_audience=1 if publico_advantage else 0)
                mudou.append(f"Advantage+ {'ligado' if publico_advantage else 'desligado'}")
            dados: dict = {}
            if any(x is not None for x in (publicos_incluir, publicos_excluir, interesses, paises, regioes, cidades, idade_min,
                                           idade_max, genero, posicionamentos, publico_advantage)):
                dados["targeting"] = alvo
            if inicio:
                dados["start_time"] = inicio
                mudou.append(f"início {data_inicio}")
            if fim:
                dados["end_time"] = fim
                mudou.append(f"fim {data_fim}")
            if extras:
                dados.update(extras)
                mudou.append("campos avançados: " + ", ".join(sorted(extras)))
            try:
                await _post(c, str(id), dados)
            except MetaError as e:
                _registrar("atualizar_conjunto", str(id), params, False, str(e))
                raise
        msg = f"conjunto {obj.get('name')} ({id}): " + "; ".join(mudou)
        _registrar("atualizar_conjunto", str(id), params, True, msg)
        return f"Cliente: {nome_cliente}\nOK {msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def atualizar_campanha(
    id: Annotated[str, Field(description="ID da campanha.")],
    data_inicio: Annotated[Optional[str], Field(description="Novo início (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")] = None,
    data_fim: Annotated[Optional[str], Field(description="Novo fim (AAAA-MM-DD ou AAAA-MM-DDTHH:MM).")] = None,
    limite_gasto: Annotated[Optional[float], Field(description="Novo limite de gasto da campanha (spend cap), na moeda da conta. Exige a permissão de orçamentos e lances.", gt=0)] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Muda as datas e o limite de gasto de uma campanha da conta padrão do cliente. Datas exigem a permissão 'editar';
    limite de gasto exige 'orçamentos e lances'. Orçamento diário/total continua em alterar_orcamento."""
    try:
        nome_cliente = _usar_cliente(cliente)
        if data_inicio is None and data_fim is None and limite_gasto is None:
            raise MetaError("Informe data_inicio, data_fim ou limite_gasto.")
        if data_inicio is not None or data_fim is not None:
            _exigir("editar")
        if limite_gasto is not None:
            _exigir("orcamento")
        inicio = _data_meta(data_inicio, "data_inicio") if data_inicio else None
        fim = _data_meta(data_fim, "data_fim", fim=True) if data_fim else None
        params = {"data_inicio": data_inicio, "data_fim": data_fim, "limite_gasto": limite_gasto}
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, id, "start_time,stop_time,spend_cap")
            if tipo != "campaign":
                raise MetaError(f"Este ID é {_tipo_frase(tipo)}; atualizar_campanha só vale para campanhas.")
            dados: dict = {}
            mudou = []
            if inicio:
                dados["start_time"] = inicio
                mudou.append(f"início {data_inicio}")
            if fim:
                dados["stop_time"] = fim
                mudou.append(f"fim {data_fim}")
            if limite_gasto is not None:
                dados["spend_cap"] = _centavos(limite_gasto, conta["moeda"])
                mudou.append(f"limite de gasto {conta['moeda']} {limite_gasto:,.2f}")
            try:
                await _post(c, str(id), dados)
            except MetaError as e:
                _registrar("atualizar_campanha", str(id), params, False, str(e))
                raise
        msg = f"campanha {obj.get('name')} ({id}): " + "; ".join(mudou)
        _registrar("atualizar_campanha", str(id), params, True, msg)
        return f"Cliente: {nome_cliente}\nOK {msg}"
    except Exception as e:
        return _tool_error(e)


# ───────────────────────── 6.1.0: diagnóstico, prévia, criativos, Instagram, biblioteca, pixel, conversões, comparação ─────────────────────────

_STATUS_PROBLEMA = {"DISAPPROVED", "WITH_ISSUES", "PENDING_REVIEW", "PENDING_BILLING_INFO", "ADSET_PAUSED_ERROR", "IN_PROCESS"}
_STATUS_TXT = {"DISAPPROVED": "reprovado", "WITH_ISSUES": "com problemas", "PENDING_REVIEW": "em análise",
               "PENDING_BILLING_INFO": "falta forma de pagamento", "IN_PROCESS": "processando", "ACTIVE": "ativo",
               "PAUSED": "pausado", "CAMPAIGN_PAUSED": "campanha pausada", "ADSET_PAUSED": "conjunto pausado",
               "ARCHIVED": "arquivado", "DELETED": "excluído"}


def _txt_problemas(r: dict) -> str:
    partes = []
    for i in (r.get("issues_info") or []):
        partes.append(str(i.get("error_summary") or i.get("error_message") or i.get("level") or "problema"))
    fb = r.get("ad_review_feedback") or {}
    for bloco in (fb.get("global") or {}, ):
        for k, v in bloco.items():
            partes.append(f"revisão: {k} ({v})")
    for pos, motivos in (fb.get("placement_specific") or {}).items():
        for k, v in (motivos or {}).items():
            partes.append(f"revisão em {pos}: {k} ({v})")
    return " | ".join(dict.fromkeys(partes))


@mcp.tool(annotations=_RO)
async def diagnosticar_entrega(
    nivel: Annotated[Literal["anuncio", "conjunto"], Field(description="anuncio: reprovações e problemas; conjunto: fase de aprendizado e problemas.")] = "anuncio",
    incluir_normais: Annotated[bool, Field(description="True mostra também o que está sem problema.")] = False,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Por que algo não entrega: anúncios reprovados, em análise ou com problemas (com o motivo da Meta) e conjuntos
    com aprendizado limitado ou problemas. Só lê. Usa só o que a conta padrão do cliente tem."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            if nivel == "anuncio":
                rows = await _get_all(c, f"{conta['act']}/ads", {
                    "fields": "id,name,effective_status,issues_info,ad_review_feedback,adset_id,campaign_id", "limit": 200,
                    "effective_status": json.dumps(["ACTIVE", "PAUSED", "DISAPPROVED", "WITH_ISSUES", "PENDING_REVIEW", "PENDING_BILLING_INFO",
                                                    "CAMPAIGN_PAUSED", "ADSET_PAUSED", "IN_PROCESS"])})
            else:
                rows = await _get_all(c, f"{conta['act']}/adsets", {
                    "fields": "id,name,effective_status,issues_info,learning_stage_info,campaign_id", "limit": 200,
                    "effective_status": json.dumps(["ACTIVE", "PAUSED", "WITH_ISSUES", "CAMPAIGN_PAUSED", "IN_PROCESS", "PENDING_BILLING_INFO"])})
        linhas = []
        for r in rows:
            st = r.get("effective_status") or ""
            prob = _txt_problemas(r)
            apr = (r.get("learning_stage_info") or {}).get("status")
            ruim = st in _STATUS_PROBLEMA or bool(prob) or apr == "FAIL"
            if not (ruim or incluir_normais):
                continue
            linha = {"id": r.get("id"), "nome": r.get("name"), "situacao": _STATUS_TXT.get(st, st), "problemas": prob or None,
                     "campanha_id": r.get("campaign_id")}
            if nivel == "conjunto":
                linha["aprendizado"] = {"LEARNING": "aprendendo", "SUCCESS": "concluído", "FAIL": "aprendizado limitado"}.get(apr, apr)
            else:
                linha["conjunto_id"] = r.get("adset_id")
            linhas.append(linha)
        cols = ["id", "nome", "situacao", "problemas", "campanha_id"] + (["aprendizado"] if nivel == "conjunto" else ["conjunto_id"])
        topo = f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} de {len(rows)} {nivel}(s) " + \
               ("listados" if incluir_normais else "com algo a resolver")
        return topo + "\n" + (_csv(linhas, cols) if linhas else "Nada com problema nos itens ativos, pausados ou em análise.\n")
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def log_atividades(
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias atrás.")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD. Vazio: hoje.")] = None,
    limite: Annotated[int, Field(description="Máximo de eventos.", ge=1, le=500)] = 100,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Histórico de alterações feito na conta padrão do cliente (quem mudou o quê e quando), direto da Meta. Serve para
    descobrir por que o desempenho mudou. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        d2 = _parse_date(ate, "ate") if ate else date.today()
        d1 = _parse_date(desde, "desde") if desde else d2 - timedelta(days=7)
        if d1 > d2:
            raise MetaError("'desde' não pode ser depois de 'ate'.")
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            dados = await _get(c, f"{conta['act']}/activities", {
                "fields": "event_time,event_type,translated_event_type,object_id,object_name,object_type,actor_name,extra_data",
                "since": d1.isoformat(), "until": (d2 + timedelta(days=1)).isoformat(), "limit": limite})
        rows = (dados.get("data") or [])[:limite]
        linhas = [{"quando": (r.get("event_time") or "")[:16].replace("T", " "), "quem": r.get("actor_name"),
                   "o_que": r.get("translated_event_type") or r.get("event_type"), "tipo": r.get("object_type"),
                   "objeto": r.get("object_name"), "objeto_id": r.get("object_id"),
                   "detalhe": _scrub(str(r.get("extra_data") or ""))[:200]} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {d1} a {d2} | {len(linhas)} evento(s)\n" + _csv(
            linhas, ["quando", "quem", "o_que", "tipo", "objeto", "objeto_id", "detalhe"])
    except Exception as e:
        return _tool_error(e)


_FORMATOS_PREVIA = {"feed_facebook": "DESKTOP_FEED_STANDARD", "feed_mobile": "MOBILE_FEED_STANDARD", "instagram_feed": "INSTAGRAM_STANDARD",
                    "instagram_stories": "INSTAGRAM_STORY", "instagram_reels": "INSTAGRAM_REELS", "facebook_stories": "FACEBOOK_STORY_MOBILE",
                    "facebook_reels": "FACEBOOK_REELS_MOBILE"}


@mcp.tool(annotations=_RO)
async def previa_anuncio(
    anuncio_id: Annotated[str, Field(description="ID do anúncio da conta padrão.")],
    formato: Annotated[Literal["feed_facebook", "feed_mobile", "instagram_feed", "instagram_stories", "instagram_reels",
                               "facebook_stories", "facebook_reels"], Field(description="Onde mostrar.")] = "instagram_feed",
    cliente: _CLIENTE = None,
) -> str:
    """Link de prévia de como o anúncio aparece num posicionamento (abre no navegador). Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, anuncio_id)
            if tipo != "ad":
                raise MetaError("previa_anuncio só vale para anúncios (não campanha ou conjunto).")
            dados = await _get(c, f"{anuncio_id}/previews", {"ad_format": _FORMATOS_PREVIA[formato]})
        itens = dados.get("data") or []
        if not itens:
            return f"Cliente: {nome_cliente} | A Meta não devolveu prévia para '{obj.get('name')}' em {formato} (formato não aceito por esse anúncio)."
        m = re.search(r'src="([^"]+)"', str(itens[0].get("body") or ""))
        if not m:
            return f"Cliente: {nome_cliente} | A prévia veio sem link utilizável."
        return f"Cliente: {nome_cliente} | Anúncio: {obj.get('name')} | Formato: {formato}\nPrévia: {html.unescape(m.group(1))}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_criativos(
    limite: Annotated[int, Field(description="Máximo de criativos.", ge=1, le=200)] = 50,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Criativos da conta padrão do cliente (texto, título, botão, miniatura, post de origem). Para reaproveitar em novos anúncios."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            dados = await _get(c, f"{conta['act']}/adcreatives", {
                "fields": "id,name,status,object_type,title,body,call_to_action_type,thumbnail_url,effective_object_story_id", "limit": limite})
        rows = (dados.get("data") or [])[:limite]
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "status": r.get("status"), "tipo": r.get("object_type"),
                   "titulo": r.get("title"), "texto": (r.get("body") or "")[:120], "botao": r.get("call_to_action_type"),
                   "post": r.get("effective_object_story_id"), "miniatura": r.get("thumbnail_url")} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} criativo(s)\n" + _csv(
            linhas, ["id", "nome", "status", "tipo", "titulo", "texto", "botao", "post", "miniatura"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_instagram(
    instagram_id: Annotated[Optional[str], Field(description="Vazio lista os perfis do Instagram da conta; com ID lista os posts recentes desse perfil.")] = None,
    limite: Annotated[int, Field(description="Máximo de posts.", ge=1, le=50)] = 20,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Perfis do Instagram ligados à conta padrão do cliente e, com instagram_id, os posts recentes (ID, legenda, link, curtidas).
    O ID do post serve em criar_criativo/criar_anuncio (post_instagram_id). Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            perfis = await _get_all(c, f"{conta['act']}/instagram_accounts", {"fields": "id,username", "limit": 50})
            if not instagram_id:
                return f"Cliente: {nome_cliente} | Conta: {conta['nome']}\n" + _csv(
                    [{"instagram_id": p.get("id"), "usuario": p.get("username")} for p in perfis], ["instagram_id", "usuario"])
            if str(instagram_id) not in {str(p.get("id")) for p in perfis}:
                raise MetaError(f"O perfil {instagram_id} não está ligado à conta padrão do cliente. Use listar_instagram sem ID para ver os liberados.")
            dados = await _get(c, f"{instagram_id}/media", {
                "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count", "limit": limite})
        rows = (dados.get("data") or [])[:limite]
        linhas = [{"post_id": r.get("id"), "data": (r.get("timestamp") or "")[:10], "tipo": r.get("media_type"),
                   "legenda": (r.get("caption") or "").replace("\n", " ")[:100], "curtidas": r.get("like_count"),
                   "comentarios": r.get("comments_count"), "link": r.get("permalink")} for r in rows]
        return f"Cliente: {nome_cliente} | Instagram {instagram_id} | {len(linhas)} post(s)\n" + _csv(
            linhas, ["post_id", "data", "tipo", "legenda", "curtidas", "comentarios", "link"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def biblioteca_anuncios(
    termo: Annotated[str, Field(description="Palavra ou marca a buscar nos anúncios de qualquer anunciante.", min_length=2, max_length=100)],
    paises: Annotated[Optional[list[str]], Field(description="Códigos de país (ex.: BR). Vazio: BR.")] = None,
    so_ativos: Annotated[bool, Field(description="True só anúncios no ar agora.")] = True,
    limite: Annotated[int, Field(description="Máximo de anúncios.", ge=1, le=100)] = 25,
    cliente: _CLIENTE = None,
) -> str:
    """Pesquisa na Biblioteca de Anúncios da Meta (pública): o que concorrentes estão rodando, desde quando, com link para ver
    o anúncio. Só lê. Exige que o dono do token tenha a autorização da Biblioteca de Anúncios na Meta."""
    try:
        nome_cliente = _usar_cliente(cliente)
        ps = [p.strip().upper() for p in (paises or ["BR"])]
        if not all(re.fullmatch(r"[A-Z]{2}", p) for p in ps):
            raise MetaError("paises deve ter códigos de 2 letras, como BR.")
        async with _client() as c:
            dados = await _get(c, "ads_archive", {
                "search_terms": termo, "ad_reached_countries": json.dumps(ps), "ad_type": "ALL",
                "ad_active_status": "ACTIVE" if so_ativos else "ALL", "limit": limite,
                "fields": "id,page_name,ad_creative_bodies,ad_delivery_start_time,ad_snapshot_url"})
        rows = (dados.get("data") or [])[:limite]
        linhas = [{"id": r.get("id"), "anunciante": r.get("page_name"), "desde": (r.get("ad_delivery_start_time") or "")[:10],
                   "texto": ((r.get("ad_creative_bodies") or [""])[0] or "").replace("\n", " ")[:140],
                   "ver": r.get("ad_snapshot_url")} for r in rows]
        return f"Cliente: {nome_cliente} | Biblioteca de Anúncios: '{termo}' em {','.join(ps)} | {len(linhas)} anúncio(s)\n" + _csv(
            linhas, ["id", "anunciante", "desde", "texto", "ver"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def diagnosticar_pixel(
    pixel_id: Annotated[Optional[str], Field(description="ID do pixel. Vazio: o primeiro pixel da conta padrão.")] = None,
    dias: Annotated[int, Field(description="Janela em dias.", ge=1, le=30)] = 7,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Saúde de um pixel: quando disparou por último e quantos eventos recebeu por tipo nos últimos dias (PageView, Purchase etc.).
    Aponta pixel parado ou sem Purchase. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name,last_fired_time,is_unavailable", "limit": 50})
            if not pixels:
                return f"Cliente: {nome_cliente} | Conta: {conta['nome']}\nA conta não tem pixel."
            if pixel_id:
                px = next((p for p in pixels if str(p.get("id")) == str(pixel_id)), None)
                if not px:
                    raise MetaError(f"O pixel {pixel_id} não é da conta padrão do cliente. Use listar_pixels para ver os liberados.")
            else:
                px = pixels[0]
            ini = int((datetime.now(timezone.utc) - timedelta(days=dias)).timestamp())
            est = await _get(c, f"{px['id']}/stats", {"aggregation": "event", "start_time": ini})
        total: dict = {}
        for janela in est.get("data") or []:
            for e in janela.get("data") or []:
                total[e.get("value")] = total.get(e.get("value"), 0) + int(e.get("count") or 0)
        linhas = [{"evento": k, "recebidos": v} for k, v in sorted(total.items(), key=lambda kv: -kv[1])]
        avisos = []
        if px.get("is_unavailable"):
            avisos.append("o pixel está indisponível")
        if not total:
            avisos.append(f"nenhum evento nos últimos {dias} dias: o pixel está parado ou fora do site")
        elif "Purchase" not in total:
            avisos.append("há eventos, mas nenhum Purchase: campanhas de vendas não conseguem otimizar")
        topo = f"Cliente: {nome_cliente} | Pixel: {px.get('name')} ({px['id']}) | último disparo: {(px.get('last_fired_time') or 'nunca')[:16]} | últimos {dias} dias"
        return topo + "".join(f"\nATENÇÃO: {a}" for a in avisos) + "\n" + _csv(linhas, ["evento", "recebidos"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_conversoes_personalizadas(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Conversões personalizadas da conta padrão do cliente (regra, evento, último disparo, se está arquivada). Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/customconversions", {
                "fields": "id,name,custom_event_type,rule,is_archived,last_fired_time,pixel{id,name}", "limit": 100})
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "evento": r.get("custom_event_type"), "regra": _scrub(str(r.get("rule") or ""))[:160],
                   "pixel": (r.get("pixel") or {}).get("name"), "arquivada": "sim" if r.get("is_archived") else "não",
                   "ultimo_disparo": (r.get("last_fired_time") or "")[:16]} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} conversão(ões)\n" + _csv(
            linhas, ["id", "nome", "evento", "regra", "pixel", "arquivada", "ultimo_disparo"])
    except Exception as e:
        return _tool_error(e)


_METRICAS_COMPARAR = ["gasto", "impressoes", "alcance", "cliques_link", "compras", "valor_compras", "cpm", "ctr_link", "cpc_link", "cpa", "roas"]


@mcp.tool(annotations=_RO)
async def comparar_periodos(
    desde_a: Annotated[str, Field(description="Início do período A (o novo) AAAA-MM-DD.")],
    ate_a: Annotated[str, Field(description="Fim do período A AAAA-MM-DD.")],
    desde_b: Annotated[str, Field(description="Início do período B (o de comparação) AAAA-MM-DD.")],
    ate_b: Annotated[str, Field(description="Fim do período B AAAA-MM-DD.")],
    nivel: Annotated[Literal["conta", "campanha", "conjunto", "anuncio"], Field(description="Nível da comparação.")] = "campanha",
    conta: Annotated[Optional[str], Field(description="ID da conta ou trecho do nome. Vazio usa a conta padrão.")] = None,
    max_linhas: Annotated[int, Field(description="Máximo de linhas (maiores gastos).", ge=1, le=500)] = 50,
    cliente: _CLIENTE = None,
) -> str:
    """Compara dois períodos lado a lado (A contra B) com a variação percentual de gasto, compras, ROAS, CPA, CPM, CTR etc.
    Alcance de cada período é o deduplicado daquele período. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        a1, a2 = _periodo(desde_a, ate_a)
        b1, b2 = _periodo(desde_b, ate_b)
        level = NIVEIS[nivel]
        avisos: list = []
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            sem = asyncio.Semaphore(MAX_SIMULTANEAS)
            ra, rb = await asyncio.gather(_insights(c, acc["act"], level, a1, a2, False, [], sem, None, avisos),
                                          _insights(c, acc["act"], level, b1, b2, False, [], sem, None, avisos))
        chave = {"account": None, "campaign": "campanha_id", "adset": "conjunto_id", "ad": "anuncio_id"}[level]
        nome = {"account": "conta", "campaign": "campanha", "adset": "conjunto", "ad": "anuncio"}[level]

        def k(r):
            return str(r.get(chave) or "?") if chave else "conta"

        ida = {k(r): r for r in ra}; idb = {k(r): r for r in rb}
        def var(x, y):
            if x is None or y in (None, 0):
                return None
            return round((x - y) / y * 100, 1)

        linhas = []
        for ident in sorted(set(ida) | set(idb), key=lambda i: -((ida.get(i) or {}).get("gasto") or 0)):
            ra_, rb_ = ida.get(ident) or {}, idb.get(ident) or {}
            linha = {nome: ra_.get(nome) or rb_.get(nome) or ident, "id": ident}
            for m in _METRICAS_COMPARAR:
                linha[f"{m}_A"] = ra_.get(m); linha[f"{m}_B"] = rb_.get(m); linha[f"{m}_var_pct"] = var(ra_.get(m), rb_.get(m))
            linhas.append(linha)
        cols = [nome, "id"] + [f"{m}_{s}" for m in _METRICAS_COMPARAR for s in ("A", "B", "var_pct")]
        topo = [f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}) | A = {a1} a {a2} | B = {b1} a {b2} | nível {nivel}",
                f"Linhas: {len(linhas)}" + (f" (mostrando {max_linhas})" if len(linhas) > max_linhas else ""),
                "var_pct = (A - B) / B em %. Em CPA, CPM e CPC, subir é piorar."]
        topo += [f"AVISO: {x}" for x in avisos]
        return "\n".join(topo) + "\n\n" + _csv(linhas[:max_linhas], cols)
    except Exception as e:
        return _tool_error(e)



# ───────────────────────── 6.2.0: conversão personalizada, catálogos e testes A/B ─────────────────────────

_EVENTOS_CONVERSAO = {"PURCHASE", "LEAD", "COMPLETE_REGISTRATION", "ADD_TO_CART", "ADD_PAYMENT_INFO", "INITIATED_CHECKOUT", "CONTENT_VIEW",
                      "SEARCH", "ADD_TO_WISHLIST", "CONTACT", "SUBSCRIBE", "START_TRIAL", "SUBMIT_APPLICATION", "SCHEDULE", "OTHER"}
_OPERADORES_URL = {"contem": "i_contains", "igual": "eq", "nao_contem": "i_not_contains", "comeca_com": "starts_with"}


@mcp.tool(annotations=_CRIA)
async def criar_conversao_personalizada(
    nome: Annotated[str, Field(description="Nome da conversão (ex.: 'Obrigado - Compra').", min_length=1, max_length=100)],
    url_regra: Annotated[str, Field(description="Trecho da URL que marca a conversão (ex.: 'obrigado').", min_length=1, max_length=200)],
    evento: Annotated[str, Field(description="Tipo de evento: PURCHASE, LEAD, COMPLETE_REGISTRATION, ADD_TO_CART, INITIATED_CHECKOUT, CONTACT, SUBSCRIBE, OTHER etc.")] = "PURCHASE",
    condicao: Annotated[Literal["contem", "igual", "nao_contem", "comeca_com"], Field(description="Como comparar a URL.")] = "contem",
    pixel_id: Annotated[Optional[str], Field(description="Pixel da conta. Vazio: o primeiro pixel da conta padrão.")] = None,
    valor_padrao: Annotated[Optional[float], Field(description="Valor de cada conversão, se o site não envia valor.", ge=0)] = None,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Cria uma conversão personalizada baseada em URL, ligada a um pixel da conta padrão do cliente. Exige a permissão
    'criar e duplicar'. Não apaga nem altera as existentes; nome repetido é recusado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        ev = evento.strip().upper()
        if ev not in _EVENTOS_CONVERSAO:
            raise MetaError(f"Evento inválido. Use um destes: {', '.join(sorted(_EVENTOS_CONVERSAO))}.")
        params = dict(nome=nome, url_regra=url_regra, evento=ev, condicao=condicao, pixel_id=pixel_id, valor_padrao=valor_padrao)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
            if not pixels:
                raise MetaError("A conta não tem pixel. Crie o pixel na BM antes.")
            if pixel_id:
                if str(pixel_id) not in {str(p.get("id")) for p in pixels}:
                    raise MetaError(f"O pixel {pixel_id} não é da conta padrão do cliente. Use listar_pixels.")
                pid = str(pixel_id)
            else:
                pid = str(pixels[0]["id"])
            existentes = await _get_all(c, f"{conta['act']}/customconversions", {"fields": "id,name", "limit": 200})
            if any(str(e.get("name", "")).strip().lower() == nome.strip().lower() for e in existentes):
                raise MetaError(f"Já existe uma conversão chamada '{nome}'. Escolha outro nome. Nada foi criado.")
            regra = {"and": [{"url": {_OPERADORES_URL[condicao]: url_regra}}]}
            dados = {"name": nome, "event_source_id": pid, "rule": regra, "custom_event_type": ev}
            if valor_padrao is not None:
                dados["default_conversion_value"] = valor_padrao
            r = await _post(c, f"{conta['act']}/customconversions", dados)
        msg = f"OK conversão '{nome}' criada (ID {r.get('id')}), evento {ev}, URL {condicao} '{url_regra}', pixel {pid}."
        _registrar("criar_conversao_personalizada", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


async def _catalogo_da_conta(c: httpx.AsyncClient, conta: dict, catalogo_id: str) -> dict:
    cats = await _get_all(c, f"{conta['act']}/product_catalogs", {"fields": "id,name,product_count,vertical", "limit": 50})
    achado = next((x for x in cats if str(x.get("id")) == str(catalogo_id)), None)
    if not achado:
        raise MetaError(f"O catálogo {catalogo_id} não está ligado à conta padrão do cliente. Use listar_catalogos para ver os liberados.")
    return achado


@mcp.tool(annotations=_RO)
async def listar_catalogos(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Catálogos de produtos ligados à conta padrão do cliente, com quantidade de produtos. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/product_catalogs", {"fields": "id,name,product_count,vertical", "limit": 50})
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(rows)} catálogo(s)\n" + _csv(
            [{"id": r.get("id"), "nome": r.get("name"), "produtos": r.get("product_count"), "vertical": r.get("vertical")} for r in rows],
            ["id", "nome", "produtos", "vertical"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_conjuntos_produtos(catalogo_id: Annotated[str, Field(description="ID do catálogo (de listar_catalogos).")], cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Conjuntos de produtos de um catálogo da conta padrão (filtro e quantidade). Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            cat = await _catalogo_da_conta(c, conta, catalogo_id)
            rows = await _get_all(c, f"{catalogo_id}/product_sets", {"fields": "id,name,filter,product_count", "limit": 100})
        return f"Cliente: {nome_cliente} | Catálogo: {cat.get('name')} | {len(rows)} conjunto(s) de produtos\n" + _csv(
            [{"id": r.get("id"), "nome": r.get("name"), "produtos": r.get("product_count"), "filtro": json.dumps(r.get("filter"), ensure_ascii=False) if r.get("filter") else None} for r in rows],
            ["id", "nome", "produtos", "filtro"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_produtos(
    catalogo_id: Annotated[str, Field(description="ID do catálogo (de listar_catalogos).")],
    busca: Annotated[Optional[str], Field(description="Trecho do nome do produto.", max_length=100)] = None,
    limite: Annotated[int, Field(description="Máximo de produtos.", ge=1, le=200)] = 50,
    cliente: _CLIENTE = None,
    conta: _CONTA = None,
) -> str:
    """Produtos de um catálogo da conta padrão: preço, disponibilidade e se a Meta aprovou. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            cat = await _catalogo_da_conta(c, conta, catalogo_id)
            params = {"fields": "id,retailer_id,name,price,availability,review_status,url", "limit": limite}
            if busca:
                params["filter"] = json.dumps({"name": {"i_contains": busca}})
            dados = await _get(c, f"{catalogo_id}/products", params)
        rows = (dados.get("data") or [])[:limite]
        return f"Cliente: {nome_cliente} | Catálogo: {cat.get('name')} | {len(rows)} produto(s)\n" + _csv(
            [{"id": r.get("id"), "sku": r.get("retailer_id"), "nome": r.get("name"), "preco": r.get("price"), "estoque": r.get("availability"),
              "revisao": r.get("review_status"), "url": r.get("url")} for r in rows],
            ["id", "sku", "nome", "preco", "estoque", "revisao", "url"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def diagnosticar_catalogo(catalogo_id: Annotated[str, Field(description="ID do catálogo (de listar_catalogos).")], cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Saúde de um catálogo da conta padrão: feeds com a última atualização (itens aceitos, inválidos, erros) e problemas
    apontados pela Meta. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            cat = await _catalogo_da_conta(c, conta, catalogo_id)
            feeds = await _get_all(c, f"{catalogo_id}/product_feeds", {
                "fields": "id,name,schedule,latest_upload{end_time,num_detected_items,num_persisted_items,num_invalid_items,error_count,warning_count}", "limit": 50})
            try:
                diag = (await _get(c, f"{catalogo_id}/diagnostics", {"fields": "id,type,title,severity,number_of_affected_items"})).get("data") or []
                aviso = None
            except MetaError as e:
                diag, aviso = [], f"diagnóstico de itens indisponível ({e})"
        lf = []
        for f in feeds:
            u = f.get("latest_upload") or {}
            lf.append({"feed": f.get("name"), "id": f.get("id"), "ultima_carga": (u.get("end_time") or "")[:16], "detectados": u.get("num_detected_items"),
                       "aceitos": u.get("num_persisted_items"), "invalidos": u.get("num_invalid_items"), "erros": u.get("error_count"), "avisos": u.get("warning_count")})
        ld = [{"gravidade": d.get("severity"), "problema": d.get("title") or d.get("type"), "itens_afetados": d.get("number_of_affected_items")} for d in diag]
        txt = [f"Cliente: {nome_cliente} | Catálogo: {cat.get('name')} ({catalogo_id}) | {cat.get('product_count')} produto(s)"]
        if not feeds:
            txt.append("ATENÇÃO: nenhum feed configurado (produtos só entram à mão ou por pixel).")
        elif any((f.get("invalidos") or 0) > 0 or (f.get("erros") or 0) > 0 for f in lf):
            txt.append("ATENÇÃO: há itens inválidos ou erros na última carga de algum feed.")
        if aviso:
            txt.append(f"AVISO: {aviso}")
        txt.append("\nFeeds:\n" + _csv(lf, ["feed", "id", "ultima_carga", "detectados", "aceitos", "invalidos", "erros", "avisos"]))
        if ld:
            txt.append("Problemas:\n" + _csv(ld, ["gravidade", "problema", "itens_afetados"]))
        return "\n".join(txt)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_testes_ab(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Testes A/B (estudos) da conta padrão do cliente: nome, tipo, período e células com divisão de público. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/ad_studies", {
                "fields": "id,name,type,start_time,end_time,cells{name,treatment_percentage}", "limit": 50})
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "tipo": r.get("type"), "inicio": (r.get("start_time") or "")[:10],
                   "fim": (r.get("end_time") or "")[:10],
                   "celulas": "; ".join(f"{x.get('name')} ({x.get('treatment_percentage')}%)" for x in (r.get("cells") or {}).get("data", []))} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} teste(s)\n" + _csv(linhas, ["id", "nome", "tipo", "inicio", "fim", "celulas"])
    except Exception as e:
        return _tool_error(e)



# ───────────────────────── 6.4.0: leads, regras automáticas e API de Conversões ─────────────────────────

@contextlib.asynccontextmanager
async def _cliente_pagina(c: httpx.AsyncClient, conta: dict, pagina_id: str):
    """Cliente HTTP com o token da PÁGINA (formulários e leads vivem na página). A página precisa estar liberada para a conta."""
    pid = await _pagina_da_conta(c, conta, pagina_id)
    info = await _get(c, pid, {"fields": "access_token,name"})
    tok = info.get("access_token")
    if not tok:
        raise MetaError(f"A Meta não deu acesso à página {pid}: o usuário do sistema precisa estar atribuído à página "
                        "com controle total (BM > Contas > Páginas > Adicionar pessoas) e o token precisa de pages_manage_ads "
                        "e leads_retrieval.")
    _SEGREDOS_PAGINA.add(tok)
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=httpx.Timeout(120.0, connect=20.0),
                                 transport=_TRANSPORT) as pc:
        pc._nome_pagina = info.get("name") or pid  # type: ignore[attr-defined]
        yield pc


@mcp.tool(annotations=_RO)
async def listar_formularios(
    pagina_id: Annotated[str, Field(description="Página do Facebook (de listar_paginas).")],
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Formulários instantâneos (lead ads) de uma página liberada para a conta: nome, status, quantidade de leads e perguntas. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                rows = await _get_all(pc, f"{pagina_id}/leadgen_forms", {"fields": "id,name,status,leads_count,created_time,questions", "limit": 100})
                nome_pag = pc._nome_pagina
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "status": r.get("status"), "leads": r.get("leads_count"),
                   "criado": (r.get("created_time") or "")[:10],
                   "perguntas": ", ".join(str(q.get("key") or q.get("type")) for q in (r.get("questions") or []))} for r in rows]
        return f"Cliente: {nome_cliente} | Página: {nome_pag} | {len(linhas)} formulário(s)\n" + _csv(linhas, ["id", "nome", "status", "leads", "criado", "perguntas"])
    except Exception as e:
        return _tool_error(e)


def _mascara(valor: str) -> str:
    v = str(valor)
    if "@" in v:
        u, _, d = v.partition("@")
        return (u[:1] + "***@" + d)
    d = re.sub(r"\D", "", v)
    return ("***" + d[-4:]) if len(d) >= 6 else v


@mcp.tool()
async def baixar_leads(
    formulario_id: Annotated[str, Field(description="ID do formulário (de listar_formularios).")],
    pagina_id: Annotated[str, Field(description="Página dona do formulário.")],
    desde: Annotated[Optional[str], Field(description="Só leads a partir desta data AAAA-MM-DD. Vazio: todos.")] = None,
    ate: Annotated[Optional[str], Field(description="Só leads até esta data AAAA-MM-DD. Vazio: até agora.")] = None,
    max_leads: Annotated[int, Field(description="Teto de leads baixados.", ge=1, le=20000)] = 5000,
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Baixa os leads de um formulário instantâneo para um CSV na pasta do cliente (dados pessoais: ficam só no computador,
    arquivo com permissão restrita). A resposta mostra só a contagem e uma amostra com e-mail e telefone mascarados. Não altera nada na Meta."""
    try:
        nome_cliente = _usar_cliente(cliente)
        if not re.fullmatch(r"\d{5,25}", str(formulario_id)):
            raise MetaError(f"ID de formulário inválido: {formulario_id!r}.")
        d1 = _parse_date(desde, "desde") if desde else None
        d2 = _parse_date(ate, "ate") if ate else None
        if d1 and d2 and d1 > d2:
            raise MetaError("'desde' não pode ser depois de 'ate'.")
        filtros = []
        if d1:
            filtros.append({"field": "time_created", "operator": "GREATER_THAN", "value": int(datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc).timestamp()) - 1})
        if d2:
            filtros.append({"field": "time_created", "operator": "LESS_THAN", "value": int(datetime(d2.year, d2.month, d2.day, tzinfo=timezone.utc).timestamp()) + 86400})
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                forms = await _get_all(pc, f"{pagina_id}/leadgen_forms", {"fields": "id,name", "limit": 100})
                form = next((f for f in forms if str(f.get("id")) == str(formulario_id)), None)
                if not form:
                    raise MetaError(f"O formulário {formulario_id} não é da página {pagina_id}. Use listar_formularios.")
                params = {"fields": "id,created_time,field_data,ad_id,ad_name,adset_name,campaign_name", "limit": 200}
                if filtros:
                    params["filtering"] = json.dumps(filtros)
                leads, p = [], dict(params)
                while len(leads) < max_leads:
                    pag = await _get(pc, f"{formulario_id}/leads", p)
                    leads += pag.get("data") or []
                    nxt = ((pag.get("paging") or {}).get("cursors") or {}).get("after")
                    if not (pag.get("data") and (pag.get("paging") or {}).get("next") and nxt):
                        break
                    p = dict(params, after=nxt)
        leads = leads[:max_leads]
        campos: list = []
        linhas = []
        for l in leads:
            r = {"lead_id": l.get("id"), "criado": (l.get("created_time") or "")[:19], "anuncio": l.get("ad_name"),
                 "conjunto": l.get("adset_name"), "campanha": l.get("campaign_name")}
            for fd in l.get("field_data") or []:
                k = str(fd.get("name"))
                if k not in campos:
                    campos.append(k)
                r[k] = ", ".join(str(x) for x in (fd.get("values") or []))
            linhas.append(r)
        cols = ["lead_id", "criado", "anuncio", "conjunto", "campanha"] + campos
        arq = _export_dir() / f"leads-{formulario_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        fd_ = os.open(str(arq), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd_, "w", encoding="utf-8", newline="") as f:
            f.write(_csv_bruto(linhas, cols))
        amostra = [{k: (_mascara(v) if k in campos and isinstance(v, str) else v) for k, v in r.items() if k in ["lead_id", "criado"] + campos} for r in linhas[:5]]
        topo = (f"Cliente: {nome_cliente} | Formulário: {form.get('name')} | {len(linhas)} lead(s)"
                + (f" (limitado a {max_leads}; use desde/ate para o resto)" if len(leads) >= max_leads else "") + f"\nArquivo: {arq}\nAmostra (mascarada):\n")
        return topo + (_csv(amostra, ["lead_id", "criado"] + campos) if amostra else "Nenhum lead no período.\n")
    except Exception as e:
        return _tool_error(e)


def _csv_bruto(rows: list, cols: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else str(r[c]) for c in cols])
    return buf.getvalue()


_PERGUNTAS_PADRAO = {"nome": "FULL_NAME", "email": "EMAIL", "telefone": "PHONE", "cidade": "CITY", "estado": "STATE", "cpf": "CPF"}


@mcp.tool(annotations=_CRIA)
async def criar_formulario_leads(
    pagina_id: Annotated[str, Field(description="Página do Facebook (de listar_paginas).")],
    nome: Annotated[str, Field(description="Nome do formulário.", min_length=1, max_length=200)],
    politica_privacidade_url: Annotated[str, Field(description="URL https da política de privacidade (a Meta exige).")],
    perguntas: Annotated[list[str], Field(description="Campos pedidos: nome, email, telefone, cidade, estado, cpf.", min_length=1, max_length=10)] = ["nome", "email", "telefone"],
    url_agradecimento: Annotated[Optional[str], Field(description="Link do botão da tela final (ex.: grupo do WhatsApp). Vazio: a tela final fecha o formulário.")] = None,
    titulo_agradecimento: Annotated[str, Field(max_length=60)] = "Obrigado!",
    texto_agradecimento: Annotated[str, Field(max_length=200)] = "Recebemos seus dados e entraremos em contato.",
    idioma: Annotated[str, Field(description="Idioma do formulário.")] = "pt_BR",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria um formulário instantâneo (lead ad) na página liberada da conta. Exige a permissão 'criar e duplicar'. O formulário
    não gasta nada sozinho: só é usado quando um anúncio de leads o referencia. Nome repetido na página é recusado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        for u, rot in ((politica_privacidade_url, "politica_privacidade_url"), (url_agradecimento, "url_agradecimento")):
            if u and not u.lower().startswith("https://"):
                raise MetaError(f"{rot} precisa começar com https://.")
        tipos = []
        for q in perguntas:
            k = q.strip().lower()
            if k not in _PERGUNTAS_PADRAO:
                raise MetaError(f"Pergunta '{q}' desconhecida. Use: {', '.join(_PERGUNTAS_PADRAO)}.")
            tipos.append({"type": _PERGUNTAS_PADRAO[k]})
        params = dict(pagina_id=pagina_id, nome=nome, perguntas=perguntas, politica=politica_privacidade_url, agradecimento=url_agradecimento)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                existentes = await _get_all(pc, f"{pagina_id}/leadgen_forms", {"fields": "id,name", "limit": 200})
                if any(str(e.get("name", "")).strip().lower() == nome.strip().lower() for e in existentes):
                    raise MetaError(f"Já existe um formulário chamado '{nome}' nesta página. Escolha outro nome. Nada foi criado.")
                dados = {"name": nome, "locale": idioma, "questions": tipos,
                         "privacy_policy": {"url": politica_privacidade_url, "link_text": "Política de privacidade"},
                         "thank_you_page": {"title": titulo_agradecimento, "body": texto_agradecimento,
                                            "button_type": "VIEW_WEBSITE" if url_agradecimento else "NONE",
                                            **({"button_text": "Continuar", "website_url": url_agradecimento} if url_agradecimento else {})},
                         "follow_up_action_url": url_agradecimento or politica_privacidade_url}
                r = await _post(pc, f"{pagina_id}/leadgen_forms", dados)
        msg = f"OK formulário '{nome}' criado (ID {r.get('id')}) com {', '.join(perguntas)}. Use o ID no anúncio de leads."
        _registrar("criar_formulario_leads", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# Regras automáticas (da própria Meta): só PAUSAR ou AVISAR. Nunca aumentam orçamento.
_METRICAS_REGRA = {"gasto": ("spent", True), "cpm": ("cpm", True), "cpc": ("cpc", True), "custo_por_resultado": ("cost_per_result", True),
                   "ctr": ("ctr", False), "frequencia": ("frequency", False), "impressoes": ("impressions", False), "cliques": ("clicks", False),
                   "alcance": ("reach", False)}
_ENTIDADE_REGRA = {"campanha": "CAMPAIGN", "conjunto": "ADSET", "anuncio": "AD"}
_OP_REGRA = {">": "GREATER_THAN", "<": "LESS_THAN"}
_JANELA_REGRA = {"hoje": "TODAY", "ontem": "YESTERDAY", "3_dias": "LAST_3_DAYS", "7_dias": "LAST_7_DAYS", "14_dias": "LAST_14_DAYS", "30_dias": "LAST_30_DAYS"}


@mcp.tool(annotations=_RO)
async def listar_regras(cliente: _CLIENTE = None, conta: _CONTA = None) -> str:
    """Regras automáticas da conta (da biblioteca de regras da Meta): nome, status, ação e frequência. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            rows = await _get_all(c, f"{conta['act']}/adrules_library", {"fields": "id,name,status,evaluation_spec,execution_spec,schedule_spec", "limit": 100})
        linhas = [{"id": r.get("id"), "nome": r.get("name"), "status": r.get("status"),
                   "acao": (r.get("execution_spec") or {}).get("execution_type"),
                   "frequencia": (r.get("schedule_spec") or {}).get("schedule_type"),
                   "condicoes": _scrub(json.dumps((r.get("evaluation_spec") or {}).get("filters"), ensure_ascii=False))[:220]} for r in rows]
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} | {len(linhas)} regra(s)\n" + _csv(linhas, ["id", "nome", "status", "acao", "frequencia", "condicoes"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_regra_automatica(
    nome: Annotated[str, Field(description="Nome da regra.", min_length=1, max_length=200)],
    entidade: Annotated[Literal["campanha", "conjunto", "anuncio"], Field(description="O que a regra avalia.")],
    metrica: Annotated[Literal["gasto", "cpm", "cpc", "custo_por_resultado", "ctr", "frequencia", "impressoes", "cliques", "alcance"], Field(description="Métrica avaliada.")],
    operador: Annotated[Literal[">", "<"], Field(description="Maior ou menor que o valor.")],
    valor: Annotated[float, Field(description="Valor limite. Dinheiro em reais (ex.: 40 = R$ 40); ctr em %.", ge=0)],
    janela: Annotated[Literal["hoje", "ontem", "3_dias", "7_dias", "14_dias", "30_dias"], Field(description="Período avaliado.")] = "3_dias",
    acao: Annotated[Literal["avisar", "pausar"], Field(description="avisar só notifica; pausar desliga o item (exige a permissão 'ativar e pausar').")] = "avisar",
    gasto_minimo: Annotated[Optional[float], Field(description="Só avalia quem já gastou pelo menos isso (evita pausar por pouco dado).", ge=0)] = None,
    ativar: Annotated[bool, Field(description="False (padrão) cria a regra DESLIGADA, para você conferir na BM. True já cria ligada (exige 'ativar e pausar').")] = False,
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria uma regra automática da Meta que roda todo dia sobre campanhas, conjuntos ou anúncios ativos da conta. Exige a permissão
    'criar e duplicar'. Só avisa ou pausa: nunca aumenta orçamento. Nasce desligada, a menos que ativar=True. Nada é apagado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if acao == "pausar" or ativar:
            _exigir("status")
        campo, dinheiro = _METRICAS_REGRA[metrica]
        v = round(valor * 100) if dinheiro else valor
        filtros = [{"field": "entity_type", "value": _ENTIDADE_REGRA[entidade], "operator": "EQUAL"},
                   {"field": "time_preset", "value": _JANELA_REGRA[janela], "operator": "EQUAL"},
                   {"field": campo, "value": v, "operator": _OP_REGRA[operador]}]
        if gasto_minimo is not None and campo != "spent":
            filtros.append({"field": "spent", "value": round(gasto_minimo * 100), "operator": "GREATER_THAN"})
        dados = {"name": nome, "status": "ENABLED" if ativar else "DISABLED",
                 "evaluation_spec": {"evaluation_type": "SCHEDULE", "filters": filtros},
                 "execution_spec": {"execution_type": "PAUSE" if acao == "pausar" else "NOTIFICATION"},
                 "schedule_spec": {"schedule_type": "DAILY"}}
        params = dict(nome=nome, entidade=entidade, metrica=metrica, operador=operador, valor=valor, janela=janela, acao=acao, ativar=ativar)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            existentes = await _get_all(c, f"{conta['act']}/adrules_library", {"fields": "id,name", "limit": 200})
            if any(str(e.get("name", "")).strip().lower() == nome.strip().lower() for e in existentes):
                raise MetaError(f"Já existe uma regra chamada '{nome}'. Escolha outro nome. Nada foi criado.")
            r = await _post(c, f"{conta['act']}/adrules_library", dados)
        msg = (f"OK regra '{nome}' criada (ID {r.get('id')}) {'LIGADA' if ativar else 'DESLIGADA (ligue na BM depois de conferir)'}: "
               f"{entidade} com {metrica} {operador} {valor} em {janela} → {acao}.")
        _registrar("criar_regra_automatica", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


_FONTES_EVENTO = {"site": "website", "whatsapp": "chat", "telefone": "phone_call", "sistema": "system_generated", "outro": "other"}
_EVENTOS_PADRAO = {"Purchase", "Lead", "CompleteRegistration", "AddToCart", "InitiateCheckout", "ViewContent", "Contact", "Subscribe", "Schedule",
                   "SubmitApplication", "AddPaymentInfo", "Search", "AddToWishlist", "StartTrial", "PageView"}


def _hash_dado(valor: str, tipo: str) -> str:
    v = str(valor).strip().lower()
    if tipo == "ph":
        v = re.sub(r"\D", "", v)
    return hashlib.sha256(v.encode()).hexdigest()


@mcp.tool(annotations=_CRIA)
async def enviar_eventos_conversao(
    pixel_id: Annotated[str, Field(description="Pixel/dataset da conta (de listar_pixels).")],
    eventos: Annotated[list[dict], Field(description=(
        "Lista de eventos (até 1000). Cada um: evento (Purchase, Lead...), e pelo menos um de email/telefone/fbc; opcionais: valor, moeda (BRL), "
        "quando (AAAA-MM-DD HH:MM, até 7 dias atrás; vazio = agora), id_evento (evita duplicar com o pixel do site), fonte (site, whatsapp, telefone, sistema, outro), url."),
        min_length=1, max_length=1000)],
    codigo_teste: Annotated[Optional[str], Field(description="Código da aba 'Testar eventos' da Meta. Com ele os eventos só aparecem no teste, não entram nos relatórios.")] = None,
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Envia vendas, leads e outros eventos do servidor para o pixel da conta (API de Conversões), por exemplo compras fechadas no
    WhatsApp. E-mail e telefone em texto puro são convertidos em hash SHA-256 aqui no computador, nada sai em claro. Exige a permissão
    'criar e duplicar'. SEM codigo_teste os eventos são REAIS e passam a contar nos relatórios e na otimização: o Claude deve confirmar com o
    usuário antes. Não apaga nada."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if not re.fullmatch(r"\d{5,25}", str(pixel_id)):
            raise MetaError(f"ID de pixel inválido: {pixel_id!r}.")
        agora = datetime.now(timezone.utc)
        dados, avisos = [], []
        for i, e in enumerate(eventos, 1):
            desc = f"Evento {i}"
            nome_ev = str(e.get("evento") or "").strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,49}", nome_ev):
                raise MetaError(f"{desc}: 'evento' inválido ({nome_ev!r}). Padrões: {', '.join(sorted(_EVENTOS_PADRAO))}.")
            ud: dict = {}
            if e.get("email"):
                ud["em"] = [_hash_dado(e["email"], "em")]
            if e.get("telefone"):
                if len(re.sub(r"\D", "", str(e["telefone"]))) < 8:
                    raise MetaError(f"{desc}: telefone curto demais. Use DDI+DDD+número (ex.: 5511999998888).")
                ud["ph"] = [_hash_dado(e["telefone"], "ph")]
            if e.get("fbc"):
                ud["fbc"] = str(e["fbc"])
            if not ud:
                raise MetaError(f"{desc}: informe email, telefone ou fbc, senão a Meta não consegue associar o evento a ninguém.")
            quando = agora
            if e.get("quando"):
                try:
                    quando = datetime.strptime(str(e["quando"]), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    raise MetaError(f"{desc}: 'quando' precisa ser AAAA-MM-DD HH:MM (horário UTC).")
            if quando < agora - timedelta(days=7) or quando > agora + timedelta(minutes=5):
                raise MetaError(f"{desc}: a Meta só aceita eventos dos últimos 7 dias, e não do futuro.")
            fonte = _FONTES_EVENTO.get(str(e.get("fonte") or "site").lower())
            if not fonte:
                raise MetaError(f"{desc}: fonte inválida. Use: {', '.join(_FONTES_EVENTO)}.")
            ev = {"event_name": nome_ev, "event_time": int(quando.timestamp()), "action_source": fonte, "user_data": ud}
            if e.get("id_evento"):
                ev["event_id"] = str(e["id_evento"])
            if fonte == "website":
                if not e.get("url"):
                    raise MetaError(f"{desc}: fonte 'site' exige a url da página (ou use fonte 'whatsapp', 'telefone' ou 'sistema').")
                ev["event_source_url"] = str(e["url"])
            if e.get("valor") is not None:
                ev["custom_data"] = {"value": float(e["valor"]), "currency": str(e.get("moeda") or "BRL").upper()}
            elif nome_ev == "Purchase":
                avisos.append(f"{desc}: Purchase sem valor (não conta para ROAS).")
            dados.append(ev)
        params = dict(pixel_id=pixel_id, eventos=len(dados), teste=bool(codigo_teste), tipos=sorted({d["event_name"] for d in dados}))
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
            if str(pixel_id) not in {str(p.get("id")) for p in pixels}:
                raise MetaError(f"O pixel {pixel_id} não é da conta {conta['id']}. Use listar_pixels. Nada foi enviado.")
            corpo = {"data": dados}
            if codigo_teste:
                corpo["test_event_code"] = codigo_teste
            r = await _post(c, f"{pixel_id}/events", corpo)
        msg = (f"OK {r.get('events_received', len(dados))} evento(s) recebido(s) pela Meta no pixel {pixel_id}"
               + (" em MODO TESTE (não entram nos relatórios)." if codigo_teste else ". Eles passam a contar nos relatórios em minutos.")
               + (" Avisos: " + " ".join(avisos[:5]) if avisos else ""))
        _registrar("enviar_eventos_conversao", str(pixel_id), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)



# ───────────────────────── 6.5.0: teste A/B, catálogo (escrita), criativos PAC/MMU/Advantage+, pixel, comentários, BM, benchmark interno ─────────────────────────

# ── Teste A/B ──
@mcp.tool(annotations=_CRIA)
async def criar_teste_ab(
    celulas: Annotated[list[dict], Field(description=(
        "2 a 5 variantes. Cada uma: {\"nome\": \"Controle\", \"id\": \"<ID de campanha, conjunto OU anúncio>\"}. Todas do mesmo tipo e da mesma conta."),
        min_length=2, max_length=5)],
    nome: Annotated[Optional[str], Field(description="Nome do teste.", max_length=200)] = None,
    inicio: Annotated[Optional[str], Field(description="Início AAAA-MM-DD (a partir de amanhã). Vazio: amanhã.")] = None,
    fim: Annotated[Optional[str], Field(description="Fim AAAA-MM-DD. Vazio: 7 dias depois do início.")] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Cria um teste A/B (split test) da Meta entre campanhas, conjuntos ou anúncios que já existem. Divide o público em partes iguais.
    Exige a permissão 'criar e duplicar'. Não cria gasto novo: só compara o que já roda. Nada é apagado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        hoje = date.today()
        d1 = _parse_date(inicio, "inicio") if inicio else hoje + timedelta(days=1)
        d2 = _parse_date(fim, "fim") if fim else d1 + timedelta(days=7)
        if d1 <= hoje:
            raise MetaError("O teste precisa começar a partir de amanhã.")
        if d2 <= d1:
            raise MetaError("'fim' precisa ser depois de 'inicio'.")
        nomes, ids = [], []
        for i, ce in enumerate(celulas, 1):
            n, ident = str(ce.get("nome") or "").strip(), str(ce.get("id") or "").strip()
            if not n or not ident:
                raise MetaError(f"Variante {i}: informe 'nome' e 'id'.")
            nomes.append(n); ids.append(ident)
        if len({x.lower() for x in nomes}) != len(nomes) or len(set(ids)) != len(ids):
            raise MetaError("Nomes e IDs das variantes não podem se repetir.")
        params = dict(celulas=[{"nome": a, "id": b} for a, b in zip(nomes, ids)], inicio=d1.isoformat(), fim=d2.isoformat(), nome=nome)
        async with _client() as c:
            tipos, contas = set(), set()
            for ident in ids:
                obj, tipo, conta_obj = await _objeto(c, ident)
                tipos.add(tipo); contas.add(conta_obj["id"])
            if len(tipos) != 1 or len(contas) != 1:
                raise MetaError("Todas as variantes precisam ser do mesmo tipo (campanha, conjunto ou anúncio) e da mesma conta. Nada foi criado.")
            tipo = tipos.pop()
            conta = await _conta_padrao(c, contas.pop())
            campo = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[tipo]
            pct = round(100 / len(ids), 2)
            cels = [{"name": n, "treatment_percentage": pct, campo: [i]} for n, i in zip(nomes, ids)]
            dados = {"name": nome or f"Teste A/B {d1.isoformat()}", "type": "SPLIT_TEST", "cells": cels,
                     "start_time": f"{d1.isoformat()}T00:00:00", "end_time": f"{d2.isoformat()}T00:00:00"}
            r = await _post(c, f"{conta['act']}/ad_studies", dados)
        msg = f"OK teste A/B criado (ID {r.get('id')}) com {len(ids)} variantes de {_TIPO_PT.get(tipo, tipo)}, de {d1} a {d2}. Veja o resultado com listar_testes_ab."
        _registrar("criar_teste_ab", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ── Catálogo (escrita) ──
async def _feed_do_catalogo(c: httpx.AsyncClient, catalogo_id: str, feed_id: str) -> dict:
    feeds = await _get_all(c, f"{catalogo_id}/product_feeds", {"fields": "id,name", "limit": 50})
    f = next((x for x in feeds if str(x.get("id")) == str(feed_id)), None)
    if not f:
        raise MetaError(f"O feed {feed_id} não é do catálogo {catalogo_id}. Use diagnosticar_catalogo para ver os feeds.")
    return f


@mcp.tool(annotations=_CRIA)
async def criar_catalogo(
    nome: Annotated[str, Field(description="Nome do catálogo.", min_length=1, max_length=100)],
    vertical: Annotated[Literal["commerce", "vehicles", "hotels", "home_listings", "flights", "destinations"], Field(description="Tipo de catálogo.")] = "commerce",
    confirmar_novo: Annotated[bool, Field(description="Já existe catálogo na conta? A Meta recomenda UM só. True confirma que quer outro mesmo assim.")] = False,
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria um catálogo vazio na BM do cliente (exige META_BUSINESS_ID na ficha) e o liga à conta de anúncios. Exige 'criar e duplicar'.
    Recusa criar um segundo catálogo sem confirmar_novo=True, porque catálogos duplicados fragmentam os sinais e pioram a entrega."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        biz = re.sub(r"\D", "", _cfg("META_BUSINESS_ID"))
        if not biz:
            raise MetaError("A ficha do cliente não tem o ID da BM (META_BUSINESS_ID). Preencha no app para criar catálogos.")
        params = dict(nome=nome, vertical=vertical)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            atuais = await _get_all(c, f"{conta['act']}/product_catalogs", {"fields": "id,name", "limit": 50})
            if any(str(a.get("name", "")).strip().lower() == nome.strip().lower() for a in atuais):
                raise MetaError(f"Já existe um catálogo chamado '{nome}'. Nada foi criado.")
            if atuais and not confirmar_novo:
                raise MetaError("A conta já tem catálogo(s): " + "; ".join(f"{a.get('name')} ({a.get('id')})" for a in atuais)
                                + ". Use um deles ou confirme com confirmar_novo=True. Nada foi criado.")
            r = await _post(c, f"{biz}/owned_product_catalogs", {"name": nome, "vertical": vertical})
        msg = (f"OK catálogo '{nome}' criado (ID {r.get('id')}). Falta ligá-lo à conta de anúncios na BM se ele não aparecer em listar_catalogos.")
        _registrar("criar_catalogo", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_feed_produtos(
    catalogo_id: Annotated[str, Field(description="ID do catálogo (de listar_catalogos).")],
    nome: Annotated[str, Field(description="Nome do feed.", min_length=1, max_length=100)],
    url: Annotated[str, Field(description="URL https do arquivo CSV/TSV/XML que a Meta vai buscar.")],
    frequencia: Annotated[Literal["HOURLY", "DAILY", "WEEKLY"], Field(description="De quanto em quanto tempo a Meta busca.")] = "DAILY",
    hora: Annotated[int, Field(description="Hora do dia (0 a 23) para DAILY/WEEKLY.", ge=0, le=23)] = 6,
    moeda: Annotated[str, Field(description="Moeda padrão dos preços.")] = "BRL",
    pais: Annotated[str, Field(description="País do feed.")] = "BR",
    dia_semana: Annotated[str, Field(description="Dia da semana para WEEKLY (ex.: MONDAY).")] = "MONDAY",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria um feed de produtos num catálogo da conta: a Meta passa a buscar o arquivo na URL, na frequência escolhida. Exige 'criar e duplicar'.
    Nome repetido é recusado. Feeds com login (usuário e senha) não são aceitos aqui."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if not url.lower().startswith("https://"):
            raise MetaError("A URL do feed precisa começar com https://.")
        sched = {"interval": frequencia, "url": url}
        if frequencia in ("DAILY", "WEEKLY"):
            sched["hour"] = hora
        if frequencia == "WEEKLY":
            sched["day_of_week"] = dia_semana.upper()
        params = dict(catalogo_id=catalogo_id, nome=nome, url=url, frequencia=frequencia)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            cat = await _catalogo_da_conta(c, conta, catalogo_id)
            feeds = await _get_all(c, f"{catalogo_id}/product_feeds", {"fields": "id,name", "limit": 50})
            if any(str(f.get("name", "")).strip().lower() == nome.strip().lower() for f in feeds):
                raise MetaError(f"Já existe um feed chamado '{nome}' neste catálogo. Nada foi criado.")
            r = await _post(c, f"{catalogo_id}/product_feeds", {"name": nome, "schedule": sched,
                                                                  "default_currency": moeda.upper(), "country": pais.upper()})
        msg = f"OK feed '{nome}' criado (ID {r.get('id')}) no catálogo '{cat.get('name')}', busca {frequencia}. Confira em diagnosticar_catalogo depois da primeira carga."
        _registrar("criar_feed_produtos", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


_TIPOS_REGRA_FEED = {"mapear_coluna": "mapping_rule", "padrao_se_vazio": "fallback_rule", "maiusculas_minusculas": "letter_case_rule"}


@mcp.tool(annotations=_CRIA)
async def criar_regra_feed(
    catalogo_id: Annotated[str, Field(description="ID do catálogo.")],
    feed_id: Annotated[str, Field(description="ID do feed (de diagnosticar_catalogo).")],
    atributo: Annotated[str, Field(description="Campo do catálogo (title, description, availability, price, brand...).", pattern=r"^[a-z_]{2,40}$")],
    tipo: Annotated[Literal["mapear_coluna", "padrao_se_vazio", "maiusculas_minusculas"], Field(description="mapear_coluna: usa outra coluna do seu arquivo; padrao_se_vazio: valor fixo quando vazio; maiusculas_minusculas: normaliza.")],
    valor: Annotated[str, Field(description="mapear_coluna: nome da coluna; padrao_se_vazio: o valor; maiusculas_minusculas: to_upper, to_lower, capitalize_all ou capitalize_first.", max_length=200)],
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria uma regra de transformação num feed (ex.: usar a coluna 'desc' como description, ou texto padrão quando vazio). Exige 'criar e duplicar'.
    Só cria: não altera nem remove regras existentes."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params_regra = {"mapear_coluna": {"map_from": valor}, "padrao_se_vazio": {"user_default_value": valor}, "maiusculas_minusculas": {"type": valor}}[tipo]
        if tipo == "maiusculas_minusculas" and valor not in ("to_upper", "to_lower", "capitalize_all", "capitalize_first"):
            raise MetaError("Para maiusculas_minusculas use to_upper, to_lower, capitalize_all ou capitalize_first.")
        params = dict(catalogo_id=catalogo_id, feed_id=feed_id, atributo=atributo, tipo=tipo, valor=valor)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            await _catalogo_da_conta(c, conta, catalogo_id)
            await _feed_do_catalogo(c, catalogo_id, feed_id)
            r = await _post(c, f"{feed_id}/rules", {"attribute": atributo, "rule_type": _TIPOS_REGRA_FEED[tipo], "params": params_regra})
        msg = f"OK regra {tipo} em '{atributo}' criada no feed {feed_id} (ID {r.get('id')})."
        _registrar("criar_regra_feed", str(feed_id), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_conjunto_produtos(
    catalogo_id: Annotated[str, Field(description="ID do catálogo.")],
    nome: Annotated[str, Field(description="Nome do conjunto de produtos.", min_length=1, max_length=100)],
    filtro: Annotated[str, Field(description="Filtro JSON da Meta, ex.: {\"price\":{\"gt\":10000}} ou {\"availability\":{\"eq\":\"in stock\"}}. Confira antes com listar_produtos.")],
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria um conjunto de produtos (dinâmico, por filtro) num catálogo da conta. Exige 'criar e duplicar'. Nome repetido é recusado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        try:
            f = json.loads(filtro)
        except ValueError:
            raise MetaError("filtro precisa ser um JSON válido.")
        if not isinstance(f, dict) or not f:
            raise MetaError("filtro precisa ser um objeto JSON não vazio.")
        params = dict(catalogo_id=catalogo_id, nome=nome, filtro=filtro)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            cat = await _catalogo_da_conta(c, conta, catalogo_id)
            sets = await _get_all(c, f"{catalogo_id}/product_sets", {"fields": "id,name", "limit": 200})
            if any(str(s.get("name", "")).strip().lower() == nome.strip().lower() for s in sets):
                raise MetaError(f"Já existe um conjunto de produtos chamado '{nome}'. Nada foi criado.")
            r = await _post(c, f"{catalogo_id}/product_sets", {"name": nome, "filter": f})
        msg = f"OK conjunto de produtos '{nome}' criado (ID {r.get('id')}) no catálogo '{cat.get('name')}'."
        _registrar("criar_conjunto_produtos", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


_CAMPOS_PRODUTO = {"nome": "title", "descricao": "description", "preco": "price", "estoque": "availability", "condicao": "condition",
                   "url": "link", "imagem": "image_link", "marca": "brand", "visibilidade": "visibility"}
_ESTOQUE_OK = {"in stock", "out of stock", "preorder", "available for order", "discontinued"}


def _item_produto(i: int, p: dict, criar: bool, moeda: str) -> dict:
    desc = f"Produto {i}"
    sku = str(p.get("sku") or "").strip()
    if not sku:
        raise MetaError(f"{desc}: informe 'sku' (o ID do produto na sua loja).")
    d: dict = {"id": sku}
    desconhecidos = set(p) - set(_CAMPOS_PRODUTO) - {"sku", "moeda"}
    if desconhecidos:
        raise MetaError(f"{desc}: campos desconhecidos {sorted(desconhecidos)}. Use: sku, {', '.join(_CAMPOS_PRODUTO)}.")
    for k, api in _CAMPOS_PRODUTO.items():
        if p.get(k) is not None:
            d[api] = str(p[k])
    if "price" in d:
        num = _numero(d["price"].replace(",", ".")) if re.search(r"[0-9]", d["price"]) else None
        if num is None or num < 0:
            raise MetaError(f"{desc}: preço inválido ({p.get('preco')!r}).")
        d["price"] = f"{num:.2f} {str(p.get('moeda') or moeda).upper()}"
    if "availability" in d and d["availability"] not in _ESTOQUE_OK:
        raise MetaError(f"{desc}: estoque inválido. Use: {', '.join(sorted(_ESTOQUE_OK))}.")
    if "visibility" in d and d["visibility"] not in ("published", "hidden"):
        raise MetaError(f"{desc}: visibilidade deve ser published ou hidden.")
    for k in ("link", "image_link"):
        if k in d and not d[k].lower().startswith("https://"):
            raise MetaError(f"{desc}: '{k}' precisa começar com https://.")
    if criar:
        faltam = [x for x in ("title", "description", "availability", "price", "link", "image_link") if x not in d]
        if faltam:
            raise MetaError(f"{desc} ({sku}): faltam {faltam} para criar o produto.")
        d.setdefault("condition", "new")
    elif len(d) < 2:
        raise MetaError(f"{desc} ({sku}): informe ao menos um campo para atualizar.")
    return d


async def _enviar_produtos(catalogo_id: str, produtos: list, criar: bool, moeda: str, conta_: Optional[str], ferramenta: str, cliente: Optional[str]) -> str:
    nome_cliente = _usar_cliente(cliente)
    _exigir("criar" if criar else "editar")
    itens = [_item_produto(i, p, criar, moeda) for i, p in enumerate(produtos, 1)]
    skus = [i["id"] for i in itens]
    if len(set(skus)) != len(skus):
        raise MetaError("Há SKUs repetidos na lista.")
    params = dict(catalogo_id=catalogo_id, skus=skus[:25], produtos=len(itens))
    async with _client() as c:
        conta = await _conta_padrao(c, conta_)
        cat = await _catalogo_da_conta(c, conta, catalogo_id)
        reqs = [{"method": "CREATE" if criar else "UPDATE", "data": i} for i in itens]
        r = await _post(c, f"{catalogo_id}/items_batch", {"allow_upsert": "true" if criar else "false", "item_type": "PRODUCT_ITEM", "requests": reqs})
    rejeitados = (r.get("validation_status") or [])
    ruins = [x for x in rejeitados if x.get("errors")]
    msg = (f"OK {len(itens)} produto(s) {'criados/atualizados' if criar else 'atualizados'} no catálogo '{cat.get('name')}' (lote {','.join(r.get('handles') or [])}). "
           "A Meta processa o lote em segundos a minutos; confira com listar_produtos."
           + (f" ATENÇÃO: {len(ruins)} item(ns) com erro de validação: {_scrub(json.dumps(ruins, ensure_ascii=False))[:300]}" if ruins else "")
           + " Se um feed também fornece estes produtos, a próxima carga do feed pode desfazer a mudança.")
    _registrar(ferramenta, str(catalogo_id), params, True, msg)
    return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"


_PRODUTOS = Annotated[list[dict], Field(description=(
    "Até 25 produtos. Campos: sku (obrigatório), nome, descricao, preco (ex.: 199,90), moeda, estoque (in stock, out of stock, preorder, "
    "available for order, discontinued), condicao, url (https), imagem (https), marca, visibilidade (published ou hidden)."), min_length=1, max_length=25)]


@mcp.tool(annotations=_CRIA)
async def criar_produtos(
    catalogo_id: Annotated[str, Field(description="ID do catálogo.")],
    produtos: _PRODUTOS,
    moeda: Annotated[str, Field(description="Moeda padrão dos preços.")] = "BRL",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria (ou atualiza, se o SKU já existir) produtos num catálogo da conta. Exige 'criar e duplicar'. Exige nome, descrição, preço, estoque, url e imagem."""
    try:
        return await _enviar_produtos(catalogo_id, produtos, True, moeda, conta, "criar_produtos", cliente)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def atualizar_produtos(
    catalogo_id: Annotated[str, Field(description="ID do catálogo.")],
    produtos: _PRODUTOS,
    moeda: Annotated[str, Field(description="Moeda padrão dos preços.")] = "BRL",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Muda campos de produtos que já existem (preço, estoque, texto, imagem). Para esconder um produto, use visibilidade=hidden; nada é apagado. Exige 'renomear e editar'.
    SKU que não existe é rejeitado, nunca criado."""
    try:
        return await _enviar_produtos(catalogo_id, produtos, False, moeda, conta, "atualizar_produtos", cliente)
    except Exception as e:
        return _tool_error(e)


# ── Criativos avançados: PAC, MMU, Advantage+ e catálogo ──
_FEATURES_AP = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
_PLATAFORMAS = {"facebook", "instagram", "messenger", "audience_network"}
_POS_CAMPOS = {"facebook": "facebook_positions", "instagram": "instagram_positions", "messenger": "messenger_positions", "audience_network": "audience_network_positions"}


def _midia(i: int, m: dict, rot: str) -> tuple:
    h, v = m.get("imagem_hash"), m.get("video_id")
    if bool(h) == bool(v):
        raise MetaError(f"{rot} {i}: informe exatamente um entre imagem_hash e video_id (veja listar_midias).")
    return ("image", str(h)) if h else ("video", str(v))


@mcp.tool(annotations=_CRIA)
async def criar_criativo_avancado(
    nome: Annotated[str, Field(description="Nome do criativo.", min_length=1, max_length=200)],
    pagina_id: Annotated[str, Field(description="Página do Facebook (de listar_paginas).")],
    formato: Annotated[Literal["pac", "mmu", "catalogo", "advantage_plus"], Field(description=(
        "pac: mídias diferentes por posicionamento; mmu: várias mídias e a Meta escolhe; catalogo: carrossel dinâmico de produtos; "
        "advantage_plus: criativo simples com melhorias automáticas da Meta."))],
    link: Annotated[Optional[str], Field(description="URL https de destino.")] = None,
    textos: Annotated[list[str], Field(description="Textos principais (até 5; no pac e advantage_plus só o primeiro conta).", max_length=5)] = [],
    titulos: Annotated[list[str], Field(description="Títulos (até 5).", max_length=5)] = [],
    descricoes: Annotated[list[str], Field(description="Descrições (até 5).", max_length=5)] = [],
    cta: Annotated[str, Field(description="Botão (ex.: SHOP_NOW, LEARN_MORE, WHATSAPP_MESSAGE).")] = "LEARN_MORE",
    instagram_id: Annotated[Optional[str], Field(description="Perfil do Instagram (de listar_paginas).")] = None,
    midias: Annotated[list[dict], Field(description=(
        "mmu: 2 a 10 itens {imagem_hash|video_id}. pac: 2 a 10 itens {imagem_hash|video_id, plataformas: [facebook, instagram...], posicoes: [feed, story, reels...]}; "
        "exatamente UM item sem plataformas é o padrão (fallback). advantage_plus: 1 item."), max_length=10)] = [],
    conjunto_produtos_id: Annotated[Optional[str], Field(description="catalogo: ID do conjunto de produtos (de listar_conjuntos_produtos).")] = None,
    melhorias: Annotated[list[str], Field(description="advantage_plus: nomes das melhorias (ex.: image_touchups, text_optimizations, enhance_cta, image_brightness_and_contrast). Vazio: o conjunto padrão da Meta.")] = [],
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria criativos avançados da Meta (PAC, MMU, catálogo dinâmico, Advantage+) numa conta liberada. Exige 'criar e duplicar'. Só cria o criativo
    (não gasta nada): use o ID no criar_anuncio (criativo_id). Os formatos seguem a documentação da Meta; como variam por objetivo, a Meta pode recusar
    uma combinação e o erro dela é repassado."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if link and not link.lower().startswith("https://"):
            raise MetaError("link precisa começar com https://.")
        if cta not in _CTAS_VALIDOS:
            raise MetaError(f"Botão inválido: {cta}. Use um destes: {', '.join(sorted(_CTAS_VALIDOS))}.")
        params = dict(nome=nome, pagina_id=pagina_id, formato=formato, link=link, midias=len(midias), conjunto_produtos_id=conjunto_produtos_id)
        story = {"page_id": str(pagina_id)}
        if instagram_id:
            story["instagram_user_id"] = str(instagram_id)
        criativo: dict = {"name": nome}
        if formato in ("pac", "mmu"):
            if not link and cta not in ("WHATSAPP_MESSAGE", "MESSAGE_PAGE"):
                raise MetaError("link é obrigatório (exceto botões de mensagem).")
            if not (2 <= len(midias) <= 10):
                raise MetaError(f"{formato}: informe de 2 a 10 itens em 'midias'.")
            if not textos:
                raise MetaError("Informe ao menos um texto principal em 'textos'.")
            imagens, videos, regras = [], [], []
            fallback = 0
            for i, m in enumerate(midias, 1):
                tipo_m, ident = _midia(i, m, "Mídia")
                rotulo = f"m{i}"
                (imagens if tipo_m == "image" else videos).append(
                    ({"hash": ident, "adlabels": [{"name": rotulo}]} if tipo_m == "image" else {"video_id": ident, "adlabels": [{"name": rotulo}]}))
                if formato == "pac":
                    plats = [str(x).lower() for x in (m.get("plataformas") or [])]
                    if not plats:
                        fallback += 1
                        spec: dict = {}
                    else:
                        if not set(plats) <= _PLATAFORMAS:
                            raise MetaError(f"Mídia {i}: plataformas aceitas: {', '.join(sorted(_PLATAFORMAS))}.")
                        spec = {"publisher_platforms": plats}
                        pos = [str(x).lower() for x in (m.get("posicoes") or [])]
                        if pos and len(plats) != 1:
                            raise MetaError(f"Mídia {i}: com 'posicoes' use uma única plataforma.")
                        if pos:
                            spec[_POS_CAMPOS[plats[0]]] = pos
                    regras.append({"customization_spec": spec, ("image_label" if tipo_m == "image" else "video_label"): {"name": rotulo}})
            if formato == "pac" and fallback != 1:
                raise MetaError("pac: exatamente UMA mídia deve ficar sem 'plataformas' (a padrão, para os demais posicionamentos).")
            afs: dict = {"bodies": [{"text": t} for t in textos[:5]], "ad_formats": ["SINGLE_IMAGE" if not videos else "SINGLE_VIDEO"] if formato == "pac" else ["AUTOMATIC_FORMAT"],
                         "call_to_action_types": [cta]}
            if imagens:
                afs["images"] = imagens
            if videos:
                afs["videos"] = videos
            if link:
                afs["link_urls"] = [{"website_url": link}]
            if titulos:
                afs["titles"] = [{"text": t} for t in titulos[:5]]
            if descricoes:
                afs["descriptions"] = [{"text": t} for t in descricoes[:5]]
            if formato == "pac":
                afs["optimization_type"] = "PLACEMENT"
                afs["asset_customization_rules"] = regras
                if imagens and videos:
                    afs["ad_formats"] = ["SINGLE_IMAGE", "SINGLE_VIDEO"]
            criativo.update(object_story_spec=story, asset_feed_spec=afs)
        elif formato == "catalogo":
            if not link:
                raise MetaError("catalogo: informe o link do site (destino padrão).")
            if not conjunto_produtos_id:
                raise MetaError("catalogo: informe conjunto_produtos_id (veja listar_conjuntos_produtos).")
            if not textos:
                raise MetaError("Informe ao menos um texto principal em 'textos'.")
            story["template_data"] = {"message": textos[0], "link": link, "name": "{{product.name}}", "description": "{{product.price}}",
                                      "call_to_action": {"type": cta}, "multi_share_end_card": False}
            criativo.update(object_story_spec=story, product_set_id=str(conjunto_produtos_id))
        else:  # advantage_plus
            if len(midias) != 1:
                raise MetaError("advantage_plus: informe exatamente 1 item em 'midias'.")
            tipo_m, ident = _midia(1, midias[0], "Mídia")
            if tipo_m == "video":
                raise MetaError("advantage_plus: use uma imagem (imagem_hash). Para vídeo, crie com criar_criativo e ligue melhorias pela BM.")
            if not (link and textos):
                raise MetaError("advantage_plus: informe link e ao menos um texto.")
            ld = {"image_hash": ident, "link": link, "message": textos[0], "call_to_action": {"type": cta, "value": {"link": link}}}
            if titulos:
                ld["name"] = titulos[0]
            if descricoes:
                ld["description"] = descricoes[0]
            story["link_data"] = ld
            feats = melhorias or ["image_touchups", "text_optimizations", "image_brightness_and_contrast", "enhance_cta"]
            if any(not _FEATURES_AP.match(f) for f in feats):
                raise MetaError("Nome de melhoria inválido: use letras minúsculas e _ (ex.: image_touchups).")
            criativo.update(object_story_spec=story, degrees_of_freedom_spec={"creative_features_spec": {f: {"enroll_status": "OPT_IN"} for f in feats}})
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            await _pagina_da_conta(c, conta, pagina_id)
            if conjunto_produtos_id:
                cats = await _get_all(c, f"{conta['act']}/product_catalogs", {"fields": "id", "limit": 50})
                achado = False
                for cat in cats:
                    sets = await _get_all(c, f"{cat['id']}/product_sets", {"fields": "id", "limit": 200})
                    if str(conjunto_produtos_id) in {str(s.get('id')) for s in sets}:
                        achado = True
                        break
                if not achado:
                    raise MetaError(f"O conjunto de produtos {conjunto_produtos_id} não é de um catálogo da conta. Use listar_conjuntos_produtos.")
            r = await _post(c, f"{conta['act']}/adcreatives", criativo)
        msg = f"OK criativo {formato.upper()} '{nome}' criado (criativo_id={r.get('id')}). Use esse ID em criar_anuncio (criativo_id)."
        _registrar("criar_criativo_avancado", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ── Pixel ──
@mcp.tool(annotations=_CRIA)
async def criar_pixel(
    nome: Annotated[str, Field(description="Nome do pixel/dataset.", min_length=1, max_length=100)],
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Cria um pixel (dataset) na conta de anúncios, se ela ainda não tiver um com esse nome. Exige 'criar e duplicar'. A Meta permite poucos pixels por conta.
    Regras de eventos por clique ou URL (Events Setup) NÃO existem na API pública: configure-as na BM ou no código do site."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            atuais = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
            if any(str(p.get("name", "")).strip().lower() == nome.strip().lower() for p in atuais):
                raise MetaError(f"Já existe um pixel chamado '{nome}'. Nada foi criado.")
            r = await _post(c, f"{conta['act']}/adspixels", {"name": nome})
        msg = f"OK pixel '{nome}' criado (ID {r.get('id')}). Instale o código no site e teste com diagnosticar_pixel."
        _registrar("criar_pixel", str(r.get("id")), dict(nome=nome), True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ── Comentários dos anúncios e posts ──
async def _post_do_anuncio(c: httpx.AsyncClient, anuncio_id: str) -> str:
    obj, tipo, _conta = await _objeto(c, anuncio_id, "creative{effective_object_story_id,effective_instagram_media_id}")
    if tipo != "ad":
        raise MetaError("anuncio_id precisa ser um anúncio.")
    cr = obj.get("creative") or {}
    pid = cr.get("effective_object_story_id") or cr.get("effective_instagram_media_id")
    if not pid:
        raise MetaError("Esse anúncio não tem post associado ainda (ou o criativo não usa um post).")
    return str(pid)


@mcp.tool(annotations=_RO)
async def listar_comentarios(
    pagina_id: Annotated[str, Field(description="Página dona do post (de listar_paginas).")],
    anuncio_id: Annotated[Optional[str], Field(description="Anúncio da conta: lê os comentários do post dele.")] = None,
    post_id: Annotated[Optional[str], Field(description="Post do Facebook ({pagina}_{post}) ou mídia do Instagram, se não usar anuncio_id.")] = None,
    plataforma: Annotated[Literal["facebook", "instagram"], Field(description="Onde está o post.")] = "facebook",
    limite: Annotated[int, Field(description="Máximo de comentários.", ge=1, le=200)] = 50,
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Comentários de um anúncio ou post (inclui posts de anúncio, que não aparecem na página). Só lê. Mostra se já estão ocultos."""
    try:
        nome_cliente = _usar_cliente(cliente)
        if bool(anuncio_id) == bool(post_id):
            raise MetaError("Informe anuncio_id OU post_id.")
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            pid = await _post_do_anuncio(c, anuncio_id) if anuncio_id else str(post_id)
            if plataforma == "facebook" and not pid.startswith(f"{pagina_id}_"):
                raise MetaError(f"O post {pid} não é da página {pagina_id}.")
            campos = "id,message,from,created_time,is_hidden,can_hide,comment_count" if plataforma == "facebook" else "id,text,username,timestamp,hidden,like_count"
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                dados = await _get(pc, f"{pid}/comments", {"fields": campos, "limit": limite})
        rows = (dados.get("data") or [])[:limite]
        if plataforma == "facebook":
            linhas = [{"id": r.get("id"), "quando": (r.get("created_time") or "")[:16], "autor": (r.get("from") or {}).get("name"),
                       "texto": (r.get("message") or "").replace("\n", " ")[:200], "oculto": "sim" if r.get("is_hidden") else "não", "respostas": r.get("comment_count")} for r in rows]
        else:
            linhas = [{"id": r.get("id"), "quando": (r.get("timestamp") or "")[:16], "autor": r.get("username"),
                       "texto": (r.get("text") or "").replace("\n", " ")[:200], "oculto": "sim" if r.get("hidden") else "não", "respostas": None} for r in rows]
        return f"Cliente: {nome_cliente} | Post {pid} | {len(linhas)} comentário(s)\n" + _csv(linhas, ["id", "quando", "autor", "texto", "oculto", "respostas"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_MUDA)
async def moderar_comentario(
    comentario_id: Annotated[str, Field(description="ID do comentário (de listar_comentarios).")],
    pagina_id: Annotated[str, Field(description="Página dona do post.")],
    ocultar: Annotated[bool, Field(description="True oculta o comentário (só você e quem escreveu veem); False volta a mostrar.")] = True,
    plataforma: Annotated[Literal["facebook", "instagram"], Field(description="Onde está o comentário.")] = "facebook",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Oculta ou volta a mostrar um comentário de anúncio ou post. Reversível: NUNCA apaga. Exige 'renomear e editar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("editar")
        if not re.fullmatch(r"[0-9_]{5,60}", str(comentario_id)):
            raise MetaError(f"ID de comentário inválido: {comentario_id!r}.")
        params = dict(comentario_id=comentario_id, ocultar=ocultar, plataforma=plataforma)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                campo = "is_hidden" if plataforma == "facebook" else "hide"
                if plataforma == "facebook":
                    dono = await _get(pc, str(comentario_id), {"fields": "id,post{id}"})
                    pid = str((dono.get("post") or {}).get("id") or "")
                    if pid and not pid.startswith(f"{pagina_id}_"):
                        raise MetaError(f"O comentário {comentario_id} não é de um post da página {pagina_id}. Nada foi alterado.")
                await _post(pc, str(comentario_id), {campo: ocultar})
        msg = f"OK comentário {comentario_id} {'ocultado' if ocultar else 'visível de novo'}."
        _registrar("moderar_comentario", str(comentario_id), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def responder_comentario(
    comentario_id: Annotated[str, Field(description="ID do comentário a responder.")],
    pagina_id: Annotated[str, Field(description="Página dona do post (responde como a página).")],
    texto: Annotated[str, Field(description="Resposta pública.", min_length=1, max_length=1000)],
    plataforma: Annotated[Literal["facebook", "instagram"], Field(description="Onde está o comentário.")] = "facebook",
    cliente: _CLIENTE = None, conta: _CONTA = None,
) -> str:
    """Responde publicamente a um comentário, como a página. Exige 'criar e duplicar'. A resposta fica visível a todos e não pode ser desfeita por esta ferramenta:
    o Claude deve mostrar o texto ao usuário antes."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        if not re.fullmatch(r"[0-9_]{5,60}", str(comentario_id)):
            raise MetaError(f"ID de comentário inválido: {comentario_id!r}.")
        params = dict(comentario_id=comentario_id, plataforma=plataforma, texto=texto)
        async with _client() as c:
            conta = await _conta_padrao(c, conta)
            async with _cliente_pagina(c, conta, pagina_id) as pc:
                if plataforma == "facebook":
                    dono = await _get(pc, str(comentario_id), {"fields": "id,post{id}"})
                    pid = str((dono.get("post") or {}).get("id") or "")
                    if pid and not pid.startswith(f"{pagina_id}_"):
                        raise MetaError(f"O comentário {comentario_id} não é de um post da página {pagina_id}. Nada foi enviado.")
                    r = await _post(pc, f"{comentario_id}/comments", {"message": texto})
                else:
                    r = await _post(pc, f"{comentario_id}/replies", {"message": texto})
        msg = f"OK resposta publicada (ID {r.get('id')})."
        _registrar("responder_comentario", str(comentario_id), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ── BM: leitura e atribuição de acesso (permissão própria 'bm') ──
def _biz_id() -> str:
    b = re.sub(r"\D", "", _cfg("META_BUSINESS_ID"))
    if not b:
        raise MetaError("A ficha do cliente não tem o ID da BM (META_BUSINESS_ID). Preencha no app.")
    return b


@mcp.tool(annotations=_RO)
async def listar_usuarios_bm(cliente: _CLIENTE = None) -> str:
    """Pessoas e usuários do sistema da BM do cliente, com a função de cada um. Só lê. Exige o ID da BM na ficha e business_management no token."""
    try:
        nome_cliente = _usar_cliente(cliente)
        biz = _biz_id()
        async with _client() as c:
            pessoas = await _get_all(c, f"{biz}/business_users", {"fields": "id,name,email,role", "limit": 100})
            try:
                sis = await _get_all(c, f"{biz}/system_users", {"fields": "id,name,role", "limit": 100})
            except MetaError:
                sis = []
        linhas = [{"tipo": "pessoa", "id": p.get("id"), "nome": p.get("name"), "email": _mascara(p.get("email") or ""), "funcao": p.get("role")} for p in pessoas]
        linhas += [{"tipo": "sistema", "id": s.get("id"), "nome": s.get("name"), "email": None, "funcao": s.get("role")} for s in sis]
        return f"Cliente: {nome_cliente} | BM {biz} | {len(linhas)} usuário(s)\n" + _csv(linhas, ["tipo", "id", "nome", "email", "funcao"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_ativos_bm(cliente: _CLIENTE = None) -> str:
    """Ativos da BM do cliente: contas de anúncios, páginas, pixels e catálogos que ela possui. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        biz = _biz_id()
        arestas = [("conta de anúncios", "owned_ad_accounts", "id,name"), ("página", "owned_pages", "id,name"),
                   ("pixel", "owned_pixels", "id,name"), ("catálogo", "owned_product_catalogs", "id,name")]
        linhas, avisos = [], []
        async with _client() as c:
            for rot, aresta, campos in arestas:
                try:
                    for r in await _get_all(c, f"{biz}/{aresta}", {"fields": campos, "limit": 100}):
                        linhas.append({"tipo": rot, "id": r.get("id"), "nome": r.get("name")})
                except MetaError as e:
                    avisos.append(f"{rot}: {e}")
        return (f"Cliente: {nome_cliente} | BM {biz} | {len(linhas)} ativo(s)\n" + "".join(f"AVISO: {a}\n" for a in avisos)
                + _csv(linhas, ["tipo", "id", "nome"]))
    except Exception as e:
        return _tool_error(e)


_TAREFAS_CONTA = {"analisar": "ANALYZE", "anunciar": "ADVERTISE", "rascunhar": "DRAFT"}
_TAREFAS_PAGINA = {"analisar": "ANALYZE", "anunciar": "ADVERTISE", "criar_conteudo": "CREATE_CONTENT", "moderar": "MODERATE"}


@mcp.tool(annotations=_CRIA)
async def dar_acesso_ativo(
    tipo_ativo: Annotated[Literal["conta_anuncios", "pagina"], Field(description="O que compartilhar.")],
    ativo_id: Annotated[str, Field(description="ID da conta de anúncios (liberada ao cliente) ou da página (de listar_paginas).")],
    usuario_id: Annotated[str, Field(description="ID do usuário da BM (de listar_usuarios_bm).")],
    tarefas: Annotated[list[str], Field(description="Conta: analisar, anunciar, rascunhar. Página: analisar, anunciar, criar_conteudo, moderar. Controle total e funções de administrador NÃO são oferecidos.", min_length=1, max_length=4)],
    cliente: _CLIENTE = None,
) -> str:
    """Dá a um usuário da BM do cliente acesso LIMITADO (analisar, anunciar, rascunhar etc.) a uma conta de anúncios ou página que o cliente já usa.
    Exige a permissão própria 'administrar a BM'. Nunca dá controle total, nunca cria administradores, nunca remove acessos."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("bm")
        biz = _biz_id()
        mapa = _TAREFAS_CONTA if tipo_ativo == "conta_anuncios" else _TAREFAS_PAGINA
        tk = []
        for t in tarefas:
            k = t.strip().lower()
            if k not in mapa:
                raise MetaError(f"Tarefa '{t}' não permitida para {tipo_ativo}. Opções: {', '.join(mapa)}.")
            tk.append(mapa[k])
        if not re.fullmatch(r"\d{5,25}", str(usuario_id)) or not re.fullmatch(r"\d{5,25}", re.sub(r"\D", "", str(ativo_id))):
            raise MetaError("IDs inválidos.")
        params = dict(tipo_ativo=tipo_ativo, ativo_id=ativo_id, usuario_id=usuario_id, tarefas=tk)
        async with _client() as c:
            pessoas = await _get_all(c, f"{biz}/business_users", {"fields": "id,name", "limit": 200})
            sis = []
            try:
                sis = await _get_all(c, f"{biz}/system_users", {"fields": "id,name", "limit": 200})
            except MetaError:
                pass
            alvo = next((u for u in pessoas + sis if str(u.get("id")) == str(usuario_id)), None)
            if not alvo:
                raise MetaError(f"O usuário {usuario_id} não é da BM {biz}. Use listar_usuarios_bm. Nada foi alterado.")
            if tipo_ativo == "conta_anuncios":
                conta = await _conta_padrao(c, ativo_id)
                caminho = f"{conta['act']}/assigned_users"
                rot = f"conta {conta['nome']} ({conta['id']})"
            else:
                conta = await _conta_padrao(c)
                await _pagina_da_conta(c, conta, ativo_id)
                caminho = f"{ativo_id}/assigned_users"
                rot = f"página {ativo_id}"
            await _post(c, caminho, {"user": str(usuario_id), "tasks": tk})
        msg = f"OK {alvo.get('name')} agora tem acesso ({', '.join(tarefas)}) à {rot}. Para tirar o acesso, use a BM."
        _registrar("dar_acesso_ativo", str(ativo_id), params, True, msg)
        return f"Cliente: {nome_cliente} | BM {biz}\n{msg}"
    except Exception as e:
        return _tool_error(e)


# ── Benchmark interno (os benchmarks de setor da Meta não existem na API pública) ──
_BENCH_METRICAS = [("cpm", "menor"), ("ctr_link", "maior"), ("cpc_link", "menor"), ("cpa", "menor"), ("roas", "maior")]


@mcp.tool(annotations=_RO)
async def benchmark_interno(
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias terminando ontem (se o cliente exige período, é obrigatório).")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD.")] = None,
    nivel: Annotated[Literal["campanha", "conjunto", "anuncio"], Field(description="O que comparar entre si.")] = "anuncio",
    gasto_minimo: Annotated[float, Field(description="Ignora quem gastou menos que isso (pouco dado).", ge=0)] = 50,
    conta: Annotated[Optional[str], Field(description="ID da conta ou trecho do nome. Vazio: conta padrão.")] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Compara cada campanha, conjunto ou anúncio com a MEDIANA da própria conta no período (CPM, CTR, CPC, CPA e ROAS) e marca quem está pior
    ou melhor que o normal da conta. NÃO é o benchmark de setor da Meta (esse é proprietário e não existe na API pública): é um comparativo interno,
    útil para achar o que puxa a conta para baixo ou para cima. Só lê."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _checar_periodo(desde, ate)
        d1, d2 = _periodo(desde, ate)
        level = NIVEIS[nivel]
        avisos: list = []
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            rows = await _insights(c, acc["act"], level, d1, d2, False, [], asyncio.Semaphore(MAX_SIMULTANEAS), None, avisos)
        base = [r for r in rows if (r.get("gasto") or 0) >= gasto_minimo]
        if len(base) < 3:
            return (f"Cliente: {nome_cliente} | Conta: {acc['nome']} | {d1} a {d2}\nSó {len(base)} item(ns) com gasto de pelo menos {gasto_minimo:.0f}: pouco para comparar. "
                    "Reduza o gasto_minimo ou aumente o período.")
        def med(k):
            v = sorted(r[k] for r in base if r.get(k) is not None)
            if not v:
                return None
            m = len(v) // 2
            return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2
        medianas = {k: med(k) for k, _ in _BENCH_METRICAS}
        nome_col = {"campaign": "campanha", "adset": "conjunto", "ad": "anuncio"}[level]
        linhas = []
        for r in sorted(base, key=lambda x: -(x.get("gasto") or 0)):
            lin = {nome_col: r.get(nome_col), "id": r.get(nome_col + "_id"), "gasto": r.get("gasto")}
            piores = melhores = 0
            for k, bom in _BENCH_METRICAS:
                v, m = r.get(k), medianas[k]
                lin[k] = v
                if v is None or not m:
                    lin[k + "_vs_mediana_pct"] = None
                    continue
                dif = round((v - m) / m * 100, 1)
                lin[k + "_vs_mediana_pct"] = dif
                melhor = dif < -25 if bom == "menor" else dif > 25
                pior = dif > 25 if bom == "menor" else dif < -25
                melhores += melhor; piores += pior
            lin["leitura"] = "acima do normal da conta" if melhores > piores else ("abaixo do normal da conta" if piores > melhores else "na média")
            linhas.append(lin)
        cols = [nome_col, "id", "gasto"] + [x for k, _ in _BENCH_METRICAS for x in (k, k + "_vs_mediana_pct")] + ["leitura"]
        topo = [f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}) | {d1} a {d2} | {len(base)} {nivel}(s) com gasto ≥ {gasto_minimo:.0f}",
                "Medianas da conta: " + "; ".join(f"{k} {medianas[k]:.2f}" for k, _ in _BENCH_METRICAS if medianas[k] is not None),
                "Comparativo INTERNO (não é benchmark de setor). Diferença de mais de 25% em cada métrica conta como melhor ou pior; em CPM, CPC e CPA, menor é melhor."]
        topo += [f"AVISO: {a}" for a in avisos]
        return "\n".join(topo) + "\n\n" + _csv(linhas, cols)
    except Exception as e:
        return _tool_error(e)



def main() -> None:
    if "--check" in sys.argv:
        print(asyncio.run(listar_clientes()))
        print(asyncio.run(verificar_conexao()))
        for n in _clientes():
            print()
            print(asyncio.run(listar_contas(n)))
        return
    mcp.run()


if __name__ == "__main__":
    main()
