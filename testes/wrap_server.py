"""Sobe o servidor real via stdio, mas com a Graph API trocada por uma falsa e lenta."""
import asyncio, sys, tempfile
from pathlib import Path
import httpx
sys.path.insert(0, sys.argv[1])
import server
tmp = Path(tempfile.mkdtemp()); (tmp / "clientes").mkdir()
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR = tmp, tmp / ".env", tmp / "clientes"
(tmp / "clientes/primos.env").write_text("META_ACCESS_TOKEN=TP\nMETA_AD_ACCOUNT_ID=111111\n")
(tmp / "clientes/donaco.env").write_text("META_ACCESS_TOKEN=TD\nMETA_AD_ACCOUNT_ID=333333\n")
OWN = {"TP": ("primos", "111111"), "TD": ("donaco", "333333")}
async def handler(req):
    cli, acc = OWN[req.headers["authorization"].removeprefix("Bearer ")]
    await asyncio.sleep(0.4 if cli == "primos" else 0.1)  # força as duas chamadas a se entrelaçarem
    p = req.url.path
    if not p.endswith(f"act_{acc}") and not p.endswith(f"act_{acc}/insights"):
        return httpx.Response(403, json={"error": {"code": 200, "message": f"token de {cli} tentou acessar {p}"}})
    if p.endswith("/insights"):
        return httpx.Response(200, json={"data": [{"account_id": acc, "campaign_id": "1", "campaign_name": f"campanha-do-{cli}", "spend": "111.00" if cli == "primos" else "333.00", "impressions": "10"}]})
    return httpx.Response(200, json={"account_id": acc, "name": f"CONTA {cli.upper()}", "currency": "BRL"})
server._TRANSPORT = httpx.MockTransport(handler)
server.mcp.run()
