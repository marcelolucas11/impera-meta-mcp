"""Proteção contra bloqueio da Meta: limites de uso, disjuntor por conta, ritmo de escrita.

Ideia central: quando a Meta diz "calma", o servidor PARA de insistir (insistir é o que gera o
sinal de abuso 613/1996 e a suspensão). Cada token (cliente) e cada conta têm o seu próprio
estado: o limite de um cliente nunca trava outro.

Nada aqui guarda token: as chaves usam só um resumo (hash) dele.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ARQUIVO = ".limites.json"  # estado dos disjuntores (sobrevive a reiniciar o Claude)

# Ajustáveis nos testes.
agora = time.time
RITMO = os.environ.get("IMPERA_SEM_RITMO") != "1"  # False nos testes: não dorme de verdade
INTERVALO_ESCRITA = 1.0  # segundos mínimos entre duas escritas na mesma conta
LEITURAS_POR_CONTA = 6  # leituras simultâneas por conta, somando todas as ferramentas
ESPERA_MAX_SEGUNDOS = 20  # o servidor só espera sozinho até isso; acima, avisa e para

# Códigos da Meta
CODIGOS_LIMITE = {4, 17, 32, 613}
SUBCODE_ABUSO = 1996
SUBCODE_BLOQUEIO_TEMP = 1487742
SUBCODE_INSIGHTS_GLOBAL = 1504022
SUBCODE_DADOS_DEMAIS = 1487534

MIN_ABUSO = 60
MIN_BLOQUEIO = 30
MIN_RAJADA = 15
RAJADA_QTD = 3
RAJADA_JANELA = 5 * 60

_memoria: dict = {}  # uso lido nos cabeçalhos: chave -> {"pct":..,"quando":..,"detalhe":..}
_limitados: dict = {}  # chave -> lista de instantes de limite recente (para a rajada)
_ultima_escrita: dict = {}
_semaforos: dict = {}
_cache_estado: dict = {"mtime": None, "dados": {}, "caminho": None}


def e_limite(code: Any) -> bool:
    return code in CODIGOS_LIMITE or (isinstance(code, int) and 80000 <= code <= 80014)


def chave(token: str, act: str = "") -> str:
    h = hashlib.sha256((token or "").encode()).hexdigest()[:12]
    return f"{h}:{act or '*'}"


def conta_do_caminho(path: str) -> str:
    p = str(path).lstrip("/")
    if p.startswith("act_"):
        return p.split("/", 1)[0]
    return ""


async def dormir(segundos: float) -> None:
    if RITMO and segundos > 0:
        await asyncio.sleep(segundos)


# ───────────────────────── estado em disco ─────────────────────────

def _ler(base: Path) -> dict:
    p = Path(base) / ARQUIVO
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        _cache_estado.update(mtime=None, dados={}, caminho=str(p))
        return {}
    if _cache_estado["mtime"] == m and _cache_estado["caminho"] == str(p):
        return _cache_estado["dados"]
    try:
        d = json.loads(p.read_text())
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    _cache_estado.update(mtime=m, dados=d, caminho=str(p))
    return d


def _gravar(base: Path, dados: dict) -> None:
    p = Path(base) / ARQUIVO
    agora_ = agora()
    dados = {k: v for k, v in dados.items() if isinstance(v, dict) and v.get("ate", 0) > agora_ - 3600}
    try:
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(dados, f, ensure_ascii=False)
        os.replace(tmp, p)
        _cache_estado.update(mtime=p.stat().st_mtime_ns, dados=dados, caminho=str(p))
    except OSError:
        _cache_estado.update(mtime=None, dados=dados, caminho=str(p))


def hora(ate: float) -> str:
    return datetime.fromtimestamp(ate).strftime("%H:%M")


# ───────────────────────── disjuntor ─────────────────────────

def bloqueio(base: Path, token: str, act: str = "") -> Optional[dict]:
    """Devolve o bloqueio ativo (da conta ou do token inteiro) ou None."""
    d = _ler(base)
    t = agora()
    k_conta = chave(token, act)
    for k in (k_conta, chave(token, "")):
        e = d.get(k)
        if e and e.get("ate", 0) > t:
            return {"ate": e["ate"], "motivo": e.get("motivo", ""), "escopo": "conta" if (act and k == k_conta) else "cliente",
                    "minutos": max(1, round((e["ate"] - t) / 60))}
    return None


def mensagem_bloqueio(b: dict) -> str:
    return (f"Proteção ativa: {b['motivo']} Parei de chamar a Meta {('nesta conta' if b['escopo'] == 'conta' else 'com este token')} "
            f"até {hora(b['ate'])} (cerca de {b['minutos']} min). Insistir só piora o bloqueio. "
            "Tente de novo depois desse horário; veja status_limites.")


def abrir(base: Path, token: str, act: str, minutos: float, motivo: str, token_inteiro: bool = False) -> float:
    k = chave(token, "" if token_inteiro else act)
    d = dict(_ler(base))
    ate = agora() + minutos * 60
    atual = d.get(k, {})
    if atual.get("ate", 0) >= ate:
        return atual["ate"]
    d[k] = {"ate": ate, "motivo": motivo}
    _gravar(base, d)
    return ate


def fechar(base: Path, token: str, act: str = "") -> None:
    d = dict(_ler(base))
    for k in (chave(token, act), chave(token, "")):
        d.pop(k, None)
    _gravar(base, d)


def registrar_erro(base: Path, token: str, act: str, code: Any, subcode: Any, estimativa_min: Optional[float] = None) -> Optional[dict]:
    """Classifica um erro da Meta e abre o disjuntor quando for caso. Devolve {'ate','motivo'} se abriu."""
    if subcode == SUBCODE_ABUSO:
        motivo = "a Meta sinalizou volume de chamadas inconsistente (sinal de abuso, código 613/1996)."
        return {"ate": abrir(base, token, act, MIN_ABUSO, motivo), "motivo": motivo}
    if code == 368 or subcode == SUBCODE_BLOQUEIO_TEMP:
        motivo = "a Meta bloqueou temporariamente esta conta/usuário (código 368)."
        return {"ate": abrir(base, token, act, MIN_BLOQUEIO, motivo, token_inteiro=True), "motivo": motivo}
    if e_limite(code):
        k = chave(token, act)
        t = agora()
        lst = [x for x in _limitados.get(k, []) if x > t - RAJADA_JANELA] + [t]
        _limitados[k] = lst
        if estimativa_min and estimativa_min > 0:
            motivo = f"a Meta pediu {round(estimativa_min)} min de espera (limite de uso)."
            return {"ate": abrir(base, token, act, estimativa_min, motivo), "motivo": motivo}
        if len(lst) >= RAJADA_QTD:
            motivo = f"{len(lst)} limites de uso seguidos em menos de 5 min."
            _limitados[k] = []
            return {"ate": abrir(base, token, act, MIN_RAJADA, motivo), "motivo": motivo}
    return None


# ───────────────────────── cabeçalhos de uso ─────────────────────────

def _json(v: Optional[str]) -> Any:
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def ler_cabecalhos(headers: Any) -> dict:
    """Extrai uso (%) e espera estimada (min) dos cabeçalhos da Meta. headers: qualquer mapeamento com .get."""
    pct = 0.0
    espera = 0.0
    partes = []

    def get(n: str) -> Optional[str]:
        try:
            return headers.get(n)
        except Exception:
            return None

    app = _json(get("x-app-usage"))
    if isinstance(app, dict):
        v = max([float(app.get(k) or 0) for k in ("call_count", "total_time", "total_cputime")] or [0])
        pct = max(pct, v)
        partes.append(f"app {v:.0f}%")
    buc = _json(get("x-business-use-case-usage"))
    if isinstance(buc, dict):
        for lista in buc.values():
            for it in lista if isinstance(lista, list) else []:
                v = max(float(it.get(k) or 0) for k in ("call_count", "total_time", "total_cputime"))
                pct = max(pct, v)
                espera = max(espera, float(it.get("estimated_time_to_regain_access") or 0))
                partes.append(f"{it.get('type', 'uso')} {v:.0f}%")
    for nome in ("x-fb-ads-insights-throttle", "x-fb-ads-insights-reach-throttle"):
        t = _json(get(nome))
        if isinstance(t, dict):
            v = max(float(t.get("app_id_util_pct") or 0), float(t.get("acc_id_util_pct") or 0))
            pct = max(pct, v)
            partes.append(f"insights {v:.0f}%")
    au = _json(get("x-ad-account-usage"))
    if isinstance(au, dict):
        v = float(au.get("acc_id_util_pct") or 0)
        pct = max(pct, v)
        partes.append(f"conta {v:.0f}%")
    return {"pct": pct, "espera_min": espera, "detalhe": ", ".join(partes)}


def anotar_uso(token: str, act: str, uso: dict) -> None:
    if uso["pct"] or uso["espera_min"] or uso["detalhe"]:
        _memoria[chave(token, act)] = {**uso, "quando": agora()}


def degrau(pct: float) -> float:
    """Espera (s) antes da próxima chamada, conforme o uso: quanto mais perto de 100%, mais devagar."""
    if pct >= 90:
        return 8.0
    if pct >= 80:
        return 3.0
    if pct >= 70:
        return 1.0
    return 0.0


def uso_atual(token: str, act: str) -> dict:
    e = _memoria.get(chave(token, act)) or {}
    if e and agora() - e.get("quando", 0) > 600:
        return {}
    return e


# ───────────────────────── ritmo ─────────────────────────

def semaforo(token: str, act: str) -> asyncio.Semaphore:
    try:
        lid = id(asyncio.get_running_loop())
    except RuntimeError:
        lid = 0
    k = (lid, chave(token, act))
    if k not in _semaforos:
        _semaforos[k] = asyncio.Semaphore(LEITURAS_POR_CONTA)
    return _semaforos[k]


async def ritmo_escrita(token: str, act: str) -> None:
    """Espaça escritas na mesma conta (várias ferramentas de escrita juntas viram fila)."""
    k = chave(token, act)
    t = agora()
    alvo = max(t, _ultima_escrita.get(k, 0.0) + INTERVALO_ESCRITA)
    _ultima_escrita[k] = alvo
    if alvo > t:
        await dormir(min(alvo - t, ESPERA_MAX_SEGUNDOS))


def resumo(base: Path, token: str, contas: list) -> dict:
    """Estado atual para a ferramenta status_limites (sem expor o token)."""
    t = agora()
    out = {"bloqueios": [], "uso": []}
    d = _ler(base)
    h = chave(token, "").split(":")[0]
    for k, v in d.items():
        if k.startswith(h + ":") and v.get("ate", 0) > t:
            out["bloqueios"].append({"conta": k.split(":", 1)[1], "ate": hora(v["ate"]), "minutos": max(1, round((v["ate"] - t) / 60)), "motivo": v.get("motivo", "")})
    for act in [""] + list(contas):
        u = uso_atual(token, act)
        if u:
            out["uso"].append({"conta": act or "geral", "pct": round(u.get("pct", 0)), "detalhe": u.get("detalhe", ""),
                               "espera_min": u.get("espera_min", 0)})
    return out
