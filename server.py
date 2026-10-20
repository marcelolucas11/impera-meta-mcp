#!/usr/bin/env python3
"""Meta Ads MCP: servidor local do Impera Meta MCP para o Claude Desktop.

Vários clientes, um token por cliente: cada um fica em clientes/<nome>.env ao lado
deste script. O token vai apenas no cabeçalho Authorization das chamadas à Graph
API: nunca aparece em URL, log ou resposta de ferramenta. Uma chamada usa somente
o token do cliente pedido; não existe token "global" que sirva de reserva.

Leitura sempre. Escrita (ativar/pausar, orçamento, renomear, criar, duplicar) só
quando o cliente tem a permissão ligada no app, só na conta padrão do cliente,
com tudo criado PAUSADO, respeitando o teto de orçamento diário e com registro
de cada ação em historico/<cliente>.jsonl. Nada é apagado.
"""

import asyncio
import contextvars
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

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
                "META_PURCHASE_ACTION", "META_APP_SECRET", "NOME", "PERMISSOES", "ORCAMENTO_MAX_DIA"}
_ATIVO: contextvars.ContextVar = contextvars.ContextVar("cliente_ativo", default=None)


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


def _scrub(text: str) -> str:
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
        params["appsecret_proof"] = hmac.new(secret.encode(), _token().encode(), hashlib.sha256).hexdigest()

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


async def _estrutura(client: httpx.AsyncClient, conta: dict, niveis: set, somente_ativos: bool,
                     sem: asyncio.Semaphore) -> dict:
    moeda = conta["moeda"]
    extra = {"effective_status": json.dumps(["ACTIVE"])} if somente_ativos else {}
    out: dict = {}

    async def campanhas():
        f = ("id,name,objective,status,effective_status,bid_strategy,daily_budget,lifetime_budget,"
             "spend_cap,created_time,start_time,stop_time")
        rows = await _get_all(client, f"{conta['act']}/campaigns", dict(fields=f, limit=200, **extra), sem)
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
        rows = await _get_all(client, f"{conta['act']}/adsets", dict(fields=f, limit=100, **extra), sem)
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
        rows = await _get_all(client, f"{conta['act']}/ads", dict(fields=f, limit=200, **extra), sem)
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


def _tool_error(e: Exception) -> str:
    if isinstance(e, MetaError):
        return f"ERRO: {_scrub(str(e))}"
    log.exception("erro inesperado")
    return f"ERRO inesperado ({type(e).__name__}): {_scrub(str(e))}"


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
            "evento_compra": v.get("META_PURCHASE_ACTION") or "auto", "pasta_relatorios": str(raiz / n),
            "permissoes": "ler" + "".join("+" + x for x in _parse_permissoes(v.get("PERMISSOES"))),
            "teto_orcamento_dia": v.get("ORCAMENTO_MAX_DIA") or "",
        } for n, v in cs.items()]
        return f"{len(rows)} cliente(s):\n" + _csv(rows, ["cliente", "nome", "conta_padrao", "evento_compra", "permissoes",
                                                         "teto_orcamento_dia", "pasta_relatorios"])
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
    somente_ativos: Annotated[bool, Field(description="True lista só o que está ativo agora.")] = True,
    max_linhas: Annotated[int, Field(description="Teto de linhas por nível na resposta.", ge=1, le=2000)] = 300,
) -> str:
    """Estrutura real da conta: orçamento diário, estratégia de lance, limite de CPA (bid_amount),
    ROAS mínimo, objetivo, status e público de cada campanha/conjunto. No nível anúncios, também o
    tipo do criativo (tipo_criativo: VIDEO, SHARE, PHOTO...) e o video_id. Valores já na moeda da conta.
    Serve para conferir os limites de CPA contra o que o nome da campanha diz."""
    try:
        nome_cliente = _usar_cliente(cliente)
        niveis = {"campanhas", "conjuntos", "anuncios"} if nivel == "tudo" else {nivel}
        async with _client() as c:
            acc = await _resolve_account(c, conta)
            est = await _estrutura(c, acc, niveis, somente_ativos, asyncio.Semaphore(MAX_SIMULTANEAS))
        partes = [f"Cliente: {nome_cliente} | Conta: {acc['nome']} ({acc['id']}), moeda {acc['moeda']}. "
                  f"{'Só itens ativos.' if somente_ativos else 'Ativos e pausados (sem arquivados).'}"]
        for k in ("campanhas", "conjuntos", "anuncios"):
            if k in est:
                itens = est[k]
                partes.append(f"\n## {k}: {len(itens)}" + (f" (mostrando {max_linhas})" if len(itens) > max_linhas else ""))
                partes.append(json.dumps([_clean(i) for i in itens[:max_linhas]], ensure_ascii=False, separators=(",", ":")))
        return "\n".join(partes)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def consultar_insights(
    cliente: _CLIENTE = None,
    conta: Annotated[Optional[str], Field(description="ID da conta ou trecho do nome. Vazio usa a conta padrão.")] = None,
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias terminando ontem.")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD. Vazio: ontem.")] = None,
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
    desde: Annotated[Optional[str], Field(description="Data inicial AAAA-MM-DD. Vazio: 7 dias terminando ontem.")] = None,
    ate: Annotated[Optional[str], Field(description="Data final AAAA-MM-DD. Vazio: ontem.")] = None,
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
    "editar": "renomear",
    "criar": "criar e duplicar (sempre pausado)",
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
        linha = json.dumps({"quando": datetime.now().isoformat(timespec="seconds"), "ferramenta": ferramenta,
                            "alvo": alvo, "parametros": limpos, "ok": ok, "resultado": resultado[:600]},
                           ensure_ascii=False)
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


async def _post(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    """POST na Graph API. Só repete quando a Meta recusou antes de processar (limite de chamadas
    ou falha de conexão); em qualquer dúvida não repete, para nunca criar nada em dobro."""
    url = f"{GRAPH_HOST}/{_api_version()}/{path.lstrip('/')}"
    form = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v))
            for k, v in data.items() if v is not None}
    secret = _cfg("META_APP_SECRET")
    if secret:
        form["appsecret_proof"] = hmac.new(secret.encode(), _token().encode(), hashlib.sha256).hexdigest()
    for attempt in range(len(_BACKOFF) + 1):
        last = attempt == len(_BACKOFF)
        try:
            r = await client.post(url, data=form)
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


async def _conta_padrao(c: httpx.AsyncClient) -> dict:
    conta = re.sub(r"\D", "", _cfg("META_AD_ACCOUNT_ID"))
    if not conta:
        raise MetaError(f"O cliente '{_cliente_id()}' não tem conta padrão. Alterações só são feitas na conta "
                        "padrão: escolha uma na ficha do cliente no app.")
    info = await _get(c, f"act_{conta}", {"fields": "account_id,name,currency"})
    return {"id": conta, "act": f"act_{conta}", "nome": info.get("name") or conta, "moeda": info.get("currency") or "BRL"}


async def _objeto(c: httpx.AsyncClient, obj_id: str, campos: str = "") -> tuple:
    """Lê o objeto, descobre o tipo e garante que ele é da conta padrão do cliente."""
    if not re.fullmatch(r"\d{5,25}", str(obj_id)):
        raise MetaError(f"ID inválido: {obj_id!r}.")
    conta = await _conta_padrao(c)
    obj = await _get(c, str(obj_id), {"fields": ",".join(x for x in ("account_id,name", campos) if x), "metadata": "1"})
    if str(obj.get("account_id")) != conta["id"]:
        raise MetaError(f"O objeto {obj_id} é da conta {obj.get('account_id')}, não da conta padrão do cliente "
                        f"'{_cliente_id()}' ({conta['nome']}, {conta['id']}). Nada foi alterado.")
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
    cliente: _CLIENTE = None,
) -> str:
    """Duplica uma campanha (com conjuntos e anúncios), um conjunto (com anúncios) ou um anúncio, na conta
    padrão do cliente. As cópias nascem PAUSADAS. Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = {"novo_nome": novo_nome, "quantidade": quantidade}
        linhas = []
        async with _client() as c:
            obj, tipo, conta = await _objeto(c, id)
            if tipo not in ("campaign", "adset", "ad"):
                raise MetaError(f"Só dá para duplicar campanha, conjunto ou anúncio (este ID é {_tipo_frase(tipo)}).")
            if tipo in ("campaign", "adset"):
                orc = await _get(c, str(id), {"fields": "daily_budget"})
                _checar_teto(_minor(orc.get("daily_budget"), conta["moeda"]), conta["moeda"])
            chave = {"campaign": "copied_campaign_id", "adset": "copied_adset_id", "ad": "copied_ad_id"}[tipo]
            for n in range(1, quantidade + 1):
                dados: dict = {"status_option": "PAUSED", "rename_options": {"rename_suffix": " - cópia"}}
                if tipo in ("campaign", "adset"):
                    dados["deep_copy"] = True
                try:
                    r = await _post(c, f"{id}/copies", dados)
                except MetaError as e:
                    _registrar("duplicar", str(id), params, False, str(e))
                    raise
                novo = str(r.get(chave) or "")
                nome_final = None
                if novo and novo_nome:
                    nome_final = novo_nome if quantidade == 1 else f"{novo_nome} {n}"
                    await _post(c, novo, {"name": nome_final})
                filhos = len(r.get("ad_object_ids") or [])
                msg = (f"cópia {n} de {_TIPO_PT.get(tipo, tipo)} {obj.get('name')} ({id}) -> ID {novo}"
                       + (f" '{nome_final}'" if nome_final else "") + (f", {filhos} objetos internos" if filhos else "") + ", PAUSADA")
                _registrar("duplicar", str(id), params, True, msg)
                linhas.append("OK " + msg)
        return f"Cliente: {nome_cliente}\n" + "\n".join(linhas) + "\nPara ativar, use alterar_status com acao='ativar'."
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_campanha(
    nome: Annotated[str, Field(description="Nome da campanha.", min_length=1, max_length=400)],
    objetivo: Annotated[Literal["OUTCOME_SALES", "OUTCOME_LEADS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_AWARENESS"],
                        Field(description="Objetivo da campanha.")] = "OUTCOME_SALES",
    orcamento_diario: Annotated[Optional[float], Field(description="Orçamento diário da campanha (CBO / Advantage+). Vazio: orçamento fica nos conjuntos.", gt=0)] = None,
    estrategia_lance: Annotated[Literal["menor_custo", "limite_custo", "limite_lance", "roas_minimo"],
                                Field(description="Estratégia de lance quando o orçamento é da campanha.")] = "menor_custo",
    cliente: _CLIENTE = None,
) -> str:
    """Cria uma campanha PAUSADA na conta padrão do cliente. Exige a permissão 'criar e duplicar'.
    Depois crie os conjuntos com criar_conjunto e os anúncios com criar_anuncio."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = {"nome": nome, "objetivo": objetivo, "orcamento_diario": orcamento_diario, "estrategia_lance": estrategia_lance}
        async with _client() as c:
            conta = await _conta_padrao(c)
            _checar_teto(orcamento_diario, conta["moeda"])
            dados: dict = {"name": nome, "objective": objetivo, "status": "PAUSED", "special_ad_categories": [],
                           "buying_type": "AUCTION"}
            if orcamento_diario is not None:
                dados["daily_budget"] = _centavos(orcamento_diario, conta["moeda"])
                dados["bid_strategy"] = _ESTRATEGIAS[estrategia_lance]
            else:
                dados["is_adset_budget_sharing_enabled"] = False
            try:
                r = await _post(c, f"{conta['act']}/campaigns", dados)
            except MetaError as e:
                _registrar("criar_campanha", conta["act"], params, False, str(e))
                raise
        msg = _ok_pausado("Campanha", str(r.get("id")), nome)
        _registrar("criar_campanha", str(r.get("id")), params, True, msg)
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']} ({conta['id']})\n{msg}"
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_CRIA)
async def criar_conjunto(
    campanha_id: Annotated[str, Field(description="ID da campanha onde o conjunto será criado.")],
    nome: Annotated[str, Field(description="Nome do conjunto.", min_length=1, max_length=400)],
    pixel_id: Annotated[Optional[str], Field(description="Pixel/dataset para otimizar (vendas e leads). Vazio: usa o único pixel da conta, se houver só um.")] = None,
    evento: Annotated[Literal["PURCHASE", "LEAD", "COMPLETE_REGISTRATION", "INITIATED_CHECKOUT", "ADD_TO_CART", "CONTENT_VIEW"],
                      Field(description="Evento de conversão a otimizar.")] = "PURCHASE",
    orcamento_diario: Annotated[Optional[float], Field(description="Orçamento diário do conjunto. Obrigatório se a campanha não tiver orçamento.", gt=0)] = None,
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
    cliente: _CLIENTE = None,
) -> str:
    """Cria um conjunto de anúncios PAUSADO numa campanha da conta padrão do cliente, otimizando para
    um evento do pixel (vendas/leads) ou visualização da página (tráfego). Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = {k: v for k, v in dict(campanha_id=campanha_id, nome=nome, pixel_id=pixel_id, evento=evento,
                  orcamento_diario=orcamento_diario, estrategia_lance=estrategia_lance, limite_cpa=limite_cpa,
                  roas_minimo=roas_minimo, paises=paises, regioes=regioes, cidades=cidades, raio_km=raio_km,
                  idade_min=idade_min, idade_max=idade_max, genero=genero, publico_advantage=publico_advantage).items()}
        if idade_min > idade_max:
            raise MetaError("A idade mínima não pode ser maior que a máxima.")
        async with _client() as c:
            camp, tipo, conta = await _objeto(c, campanha_id, "objective,daily_budget,lifetime_budget,bid_strategy")
            if tipo != "campaign":
                raise MetaError(f"{campanha_id} não é uma campanha.")
            moeda = conta["moeda"]
            objetivo = camp.get("objective")
            dados: dict = {"name": nome, "campaign_id": str(campanha_id), "status": "PAUSED", "billing_event": "IMPRESSIONS"}
            if objetivo in ("OUTCOME_SALES", "OUTCOME_LEADS"):
                if not pixel_id:
                    pixels = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name", "limit": 50})
                    if len(pixels) != 1:
                        raise MetaError("Informe o pixel_id. Pixels da conta: " + ("; ".join(f"{p.get('name')} ({p.get('id')})" for p in pixels) or "nenhum"))
                    pixel_id = str(pixels[0]["id"])
                dados.update(optimization_goal="OFFSITE_CONVERSIONS", destination_type="WEBSITE",
                             promoted_object={"pixel_id": str(pixel_id), "custom_event_type": evento})
            elif objetivo == "OUTCOME_TRAFFIC":
                dados.update(optimization_goal="LANDING_PAGE_VIEWS", destination_type="WEBSITE")
            else:
                raise MetaError(f"Criar conjunto do zero está disponível para vendas, leads e tráfego; esta campanha é "
                                f"{objetivo}. Use duplicar a partir de um conjunto existente.")
            cbo = bool(camp.get("daily_budget") or camp.get("lifetime_budget"))
            estrategia = camp.get("bid_strategy") if cbo else _ESTRATEGIAS[estrategia_lance or "menor_custo"]
            if cbo and orcamento_diario is not None:
                raise MetaError("Esta campanha tem orçamento próprio (CBO); o conjunto não leva orçamento. Ajuste o da campanha.")
            if not cbo:
                if orcamento_diario is None:
                    raise MetaError("A campanha não tem orçamento: informe orcamento_diario para o conjunto.")
                _checar_teto(orcamento_diario, moeda)
                dados["daily_budget"] = _centavos(orcamento_diario, moeda)
                dados["bid_strategy"] = estrategia
            if estrategia in ("COST_CAP", "LOWEST_COST_WITH_BID_CAP"):
                if limite_cpa is None:
                    raise MetaError("A estratégia de lance pede limite_cpa.")
                dados["bid_amount"] = _centavos(limite_cpa, moeda)
            if estrategia == "LOWEST_COST_WITH_MIN_ROAS":
                if roas_minimo is None:
                    raise MetaError("A estratégia de lance pede roas_minimo.")
                dados["bid_constraints"] = {"roas_average_floor": int(round(roas_minimo * 10000))}
            geo: dict = {"location_types": ["home", "recent"]}
            if cidades:
                geo["cities"] = [dict({"key": str(k)}, **({"radius": raio_km, "distance_unit": "kilometer"} if raio_km else {})) for k in cidades]
            if regioes:
                geo["regions"] = [{"key": str(k)} for k in regioes]
            if not cidades and not regioes:
                geo["countries"] = [p.upper() for p in paises] or ["BR"]
            alvo: dict = {"geo_locations": geo, "age_min": idade_min, "age_max": idade_max,
                          "targeting_automation": {"advantage_audience": 1 if publico_advantage else 0}}
            if genero != "todos":
                alvo["genders"] = [1] if genero == "homens" else [2]
            dados["targeting"] = alvo
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


@mcp.tool(annotations=_CRIA)
async def criar_anuncio(
    conjunto_id: Annotated[str, Field(description="ID do conjunto onde o anúncio será criado.")],
    nome: Annotated[str, Field(description="Nome do anúncio.", min_length=1, max_length=400)],
    criativo_id: Annotated[Optional[str], Field(description="Usar um criativo que já existe. Se informado, os campos abaixo são ignorados.")] = None,
    pagina_id: Annotated[Optional[str], Field(description="Página do Facebook que assina o anúncio (veja listar_paginas).")] = None,
    instagram_id: Annotated[Optional[str], Field(description="Conta do Instagram que assina o anúncio (opcional).")] = None,
    link: Annotated[Optional[str], Field(description="URL de destino.")] = None,
    texto: Annotated[Optional[str], Field(description="Texto principal.")] = None,
    titulo: Annotated[Optional[str], Field(description="Título.")] = None,
    descricao: Annotated[Optional[str], Field(description="Descrição.")] = None,
    imagem_hash: Annotated[Optional[str], Field(description="Imagem que já está na conta (veja listar_midias).")] = None,
    imagem_url: Annotated[Optional[str], Field(description="Imagem pública (https) para enviar à conta.")] = None,
    imagem_arquivo: Annotated[Optional[str], Field(description="Caminho de uma imagem neste Mac para enviar à conta.")] = None,
    video_id: Annotated[Optional[str], Field(description="Vídeo que já está na conta (veja listar_midias).")] = None,
    cta: Annotated[_CTAS, Field(description="Botão de chamada para ação.")] = "LEARN_MORE",
    parametros_url: Annotated[Optional[str], Field(description="Parâmetros de URL (UTMs), ex.: utm_source=meta&utm_campaign={{campaign.name}}.")] = None,
    cliente: _CLIENTE = None,
) -> str:
    """Cria um anúncio PAUSADO num conjunto da conta padrão do cliente, com um criativo existente ou
    montando um novo (imagem ou vídeo, texto, título, link e botão). Exige a permissão 'criar e duplicar'."""
    try:
        nome_cliente = _usar_cliente(cliente)
        _exigir("criar")
        params = dict(conjunto_id=conjunto_id, nome=nome, criativo_id=criativo_id, pagina_id=pagina_id, instagram_id=instagram_id,
                      link=link, texto=texto, titulo=titulo, descricao=descricao, imagem_hash=imagem_hash, imagem_url=imagem_url,
                      imagem_arquivo=imagem_arquivo, video_id=video_id, cta=cta, parametros_url=parametros_url)
        async with _client() as c:
            conj, tipo, conta = await _objeto(c, conjunto_id)
            if tipo != "adset":
                raise MetaError(f"{conjunto_id} não é um conjunto de anúncios.")
            try:
                if not criativo_id:
                    if not (pagina_id and link and texto):
                        raise MetaError("Para montar o criativo informe pagina_id, link e texto (ou use criativo_id).")
                    if sum(bool(x) for x in (imagem_hash, imagem_url, imagem_arquivo, video_id)) != 1:
                        raise MetaError("Informe exatamente uma mídia: imagem_hash, imagem_url, imagem_arquivo ou video_id.")
                    if imagem_url or imagem_arquivo:
                        imagem_hash = await _subir_imagem(c, conta["act"], imagem_url, imagem_arquivo)
                    botao = {"type": cta, "value": {"link": link}}
                    historia: dict = {"page_id": str(pagina_id)}
                    if instagram_id:
                        historia["instagram_user_id"] = str(instagram_id)
                    if video_id:
                        miniatura = (await _get(c, str(video_id), {"fields": "picture"})).get("picture")
                        historia["video_data"] = {k: v for k, v in {"video_id": str(video_id), "message": texto, "title": titulo,
                                                                     "link_description": descricao, "image_url": miniatura,
                                                                     "call_to_action": botao}.items() if v}
                    else:
                        historia["link_data"] = {k: v for k, v in {"link": link, "message": texto, "name": titulo,
                                                                    "description": descricao, "image_hash": imagem_hash,
                                                                    "call_to_action": botao}.items() if v}
                    criativo = {"name": f"{nome} - criativo", "object_story_spec": historia}
                    if parametros_url:
                        criativo["url_tags"] = parametros_url
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


# Leituras de apoio para montar campanhas

@mcp.tool(annotations=_RO)
async def listar_pixels(cliente: _CLIENTE = None) -> str:
    """Pixels/datasets da conta padrão do cliente (para criar conjuntos de vendas ou leads)."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c)
            rows = await _get_all(c, f"{conta['act']}/adspixels", {"fields": "id,name,last_fired_time", "limit": 50})
        return f"Cliente: {nome_cliente} | Conta: {conta['nome']}\n" + _csv(
            [{"id": r.get("id"), "nome": r.get("name"), "ultimo_disparo": (r.get("last_fired_time") or "")[:16]} for r in rows],
            ["id", "nome", "ultimo_disparo"])
    except Exception as e:
        return _tool_error(e)


@mcp.tool(annotations=_RO)
async def listar_paginas(cliente: _CLIENTE = None) -> str:
    """Páginas do Facebook (e Instagram ligado a elas) que a conta padrão do cliente pode usar nos anúncios."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c)
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
) -> str:
    """Imagens (com hash) ou vídeos (com ID) já enviados à conta padrão do cliente, mais recentes primeiro."""
    try:
        nome_cliente = _usar_cliente(cliente)
        async with _client() as c:
            conta = await _conta_padrao(c)
            if tipo == "imagens":
                rows = await _get(c, f"{conta['act']}/adimages", {"fields": "hash,name,created_time,width,height", "limit": 60})
                linhas = [{"hash": r.get("hash"), "nome": r.get("name"), "tamanho": f"{r.get('width')}x{r.get('height')}",
                           "criada": (r.get("created_time") or "")[:10]} for r in rows.get("data", [])]
                cols = ["hash", "nome", "tamanho", "criada"]
            else:
                rows = await _get(c, f"{conta['act']}/advideos", {"fields": "id,title,length,created_time", "limit": 60})
                linhas = [{"video_id": r.get("id"), "titulo": r.get("title"), "segundos": round(_f(r.get("length"))),
                           "criado": (r.get("created_time") or "")[:10]} for r in rows.get("data", [])]
                cols = ["video_id", "titulo", "segundos", "criado"]
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
