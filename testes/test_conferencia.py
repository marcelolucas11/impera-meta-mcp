"""Testa a conferência depois de escrever: relê o que criou/mudou, pausa o que nasceu ativo, e esclarece falhas
ambíguas procurando o objeto na conta (só leitura). Tudo contra uma Graph API simulada.
Uso: python testes/test_conferencia.py <raiz do repositório>"""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx

sys.path.insert(0, sys.argv[1])
for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_EXPORT_DIR"):
    os.environ.pop(k, None)
os.environ["IMPERA_SEM_RITMO"] = "1"
import server  # noqa: E402
import protecao  # noqa: E402
protecao.RITMO = False

tmp = Path(tempfile.mkdtemp())
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR, server.HISTORICO_DIR = tmp, tmp / ".env", tmp / "clientes", tmp / "historico"
server._BACKOFF = (0, 0, 0)
(tmp / "clientes").mkdir()
TOKEN, ACC, NEW, ORIG = "TOKEN_CONF_77aa", "111111111111111", "120000000000000900", "120000000000000001"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=status,orcamento,editar,criar\n")
S = {"status": {NEW: "PAUSED", ORIG: "PAUSED"}, "criar": "ok", "teimoso": False, "existe": False, "daily": "5000", "ignora_orc": False}
POSTS, URLS = [], []


def err(status, code, msg): return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req):
    URLS.append(str(req.url))
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    if req.method == "GET":
        if p == f"act_{ACC}": return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL", "timezone_name": "America/Sao_Paulo"})
        if p == f"act_{ACC}/campaigns":
            q = parse_qs(urlparse(str(req.url)).query)
            agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
            return httpx.Response(200, json={"data": [{"id": "999", "name": "Sorteio X", "status": "PAUSED", "created_time": agora}] if S["existe"] else []})
        if p in S["status"]: return httpx.Response(200, json={"id": p, "name": "obj", "status": S["status"][p], "effective_status": S["status"][p], "account_id": ACC, "objective": "OUTCOME_SALES", "daily_budget": S["daily"]})
        return err(400, 100, f"sem mock {p}")
    f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    POSTS.append((p, f))
    if p == f"act_{ACC}/campaigns":
        if S["criar"] == "500": return err(500, 2, "Service temporarily unavailable")
        S["status"][NEW] = "ACTIVE"   # a Meta ignorou o PAUSED
        return httpx.Response(200, json={"id": NEW})
    if p in S["status"] and "status" in f:
        if not S["teimoso"]: S["status"][p] = f["status"]
        return httpx.Response(200, json={"success": True})
    if p in S["status"] and "daily_budget" in f:
        if not S["ignora_orc"]: S["daily"] = f["daily_budget"]
        return httpx.Response(200, json={"success": True})
    return err(400, 100, f"POST sem mock {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail else ""))


async def main():
    out = await server.criar_campanha(nome="Sorteio X", cliente="c")
    check("campanha que a Meta devolveu ATIVA é pausada na hora e conferida", "ATENÇÃO" in out and "pausei na hora" in out and S["status"][NEW] == "PAUSED", out)
    S["status"][NEW] = "PAUSED"
    S["status"][NEW] = "PAUSED"
    S["criar"] = "ok"
    # se a Meta devolver ativa e não aceitar pausar, o aviso é alto
    S["teimoso"] = True
    out = await server.criar_campanha(nome="Sorteio X", cliente="c")
    check("se não der para pausar, avisa em ALERTA para pausar à mão", "ALERTA" in out and "PAUSE AGORA" in out, out)
    S["teimoso"] = False; S["status"][NEW] = "PAUSED"

    # alterar_status: confere
    out = await server.alterar_status(ids=[ORIG], acao="ativar", cliente="c")
    check("alterar_status relê e confirma", "[conferido na Meta]" in out and S["status"][ORIG] == "ACTIVE", out)
    S["teimoso"] = True
    out = await server.alterar_status(ids=[ORIG], acao="pausar", cliente="c")
    check("alterar_status avisa quando a Meta não aplicou", "ALERTA" in out and "ainda mostra ACTIVE" in out, out)
    S["teimoso"] = False

    # orçamento: confere
    out = await server.alterar_orcamento(id=ORIG, orcamento_diario=60, cliente="c")
    check("alterar_orcamento relê e confirma", "[conferido na Meta]" in out and S["daily"] == "6000", out)
    S["ignora_orc"] = True
    out = await server.alterar_orcamento(id=ORIG, orcamento_diario=70, cliente="c")
    check("alterar_orcamento avisa se a Meta mostra outro valor", "ATENÇÃO" in out and "daily_budget" in out, out)

    # falha ambígua na criação
    S["criar"] = "500"; S["existe"] = True
    out = await server.criar_campanha(nome="Sorteio X", cliente="c")
    check("falha ambígua: acha na conta e manda NÃO criar de novo", "JÁ EXISTE" in out and "NÃO crie de novo" in out, out)
    S["existe"] = False
    out = await server.criar_campanha(nome="Sorteio X", cliente="c")
    check("falha ambígua sem rastro: diz que é seguro tentar de novo", "nada com esse nome" in out and "seguro tentar" in out, out)
    check("o token nunca foi à URL", not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
