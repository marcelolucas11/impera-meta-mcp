"""Testa as guardas das consultas de resultados e o relatório assíncrono, contra uma Graph API simulada.
Uso: python testes/test_insights_guardas.py <raiz do repositório>"""
import asyncio, json, os, sys, tempfile
from datetime import date, timedelta
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
server._BACKOFF = (0, 0, 0); server.ASYNC_ESPERA = 0
(tmp / "clientes").mkdir()
TOKEN, ACC = "TOKEN_INS_5c5c", "111111111111111"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\n")
S = {"pesado": True, "estado": ["Job Running", "Job Completed"], "posts": 0, "gets": 0, "falha": False}
LIN = {"campaign_id": "1", "campaign_name": "Camp", "spend": "10.5", "impressions": "100", "reach": "90", "clicks": "5", "date_start": "2026-09-01", "date_stop": "2026-09-14"}
URLS = []


def err(status, code, msg): return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req):
    URLS.append(str(req.url))
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    if req.method == "POST":
        S["posts"] += 1
        return httpx.Response(200, json={"report_run_id": "555"})
    if p == f"act_{ACC}": return httpx.Response(200, json={"account_id": ACC, "name": "CA", "currency": "BRL", "timezone_name": "America/Sao_Paulo"})
    if p == f"act_{ACC}/insights":
        if S["pesado"]: return err(500, 1, "Please reduce the amount of data you're asking for, then retry your request")
        return httpx.Response(200, json={"data": [LIN]})
    if p == "555":
        S["gets"] += 1
        st = "Job Failed" if S["falha"] else S["estado"][min(S["gets"] - 1, len(S["estado"]) - 1)]
        return httpx.Response(200, json={"id": "555", "async_status": st, "async_percent_completion": 100})
    if p == "555/insights": return httpx.Response(200, json={"data": [LIN]})
    return err(400, 100, f"sem mock {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:180]}]" if detail else ""))


async def main():
    out = await server.consultar_insights(cliente="c", desde="2026-09-01", ate="2026-09-14", nivel="campanha")
    check("consulta pesada vira relatório assíncrono e devolve os dados", S["posts"] == 1 and "Camp" in out and not out.startswith("ERRO"), out)
    check("acompanhou o andamento até concluir", S["gets"] >= 2)
    S["falha"] = True; S["gets"] = 0
    out = await server.consultar_insights(cliente="c", desde="2026-09-01", ate="2026-09-14", nivel="campanha")
    check("relatório assíncrono que falha dá erro claro", out.startswith("ERRO") and "Job Failed" in out, out)
    def ins(): return len([u for u in URLS if 'insights' in u])
    n0 = ins()
    velho = (date.today() - timedelta(days=40 * 30)).isoformat()
    out = await server.consultar_insights(cliente="c", desde=velho, ate=(date.today() - timedelta(days=1)).isoformat())
    check("período além de 37 meses é recusado sem chamar a Meta", out.startswith("ERRO") and "37 meses" in out and ins() == n0, out)
    d = (date.today() - timedelta(days=500)).isoformat()
    out = await server.consultar_insights(cliente="c", desde=d, ate=(date.today() - timedelta(days=480)).isoformat(), quebra="idade_genero")
    check("quebra com mais de 13 meses é recusada", out.startswith("ERRO") and "13 meses" in out and ins() == n0, out)
    out = await server.consultar_insights(cliente="c", desde=(date.today() - timedelta(days=1000)).isoformat(), ate=(date.today() - timedelta(days=1)).isoformat(), por_dia=True)
    check("dia a dia que viraria enxurrada de chamadas é recusado", out.startswith("ERRO") and "chamadas" in out and ins() == n0, out)
    check("o token nunca foi à URL", not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
