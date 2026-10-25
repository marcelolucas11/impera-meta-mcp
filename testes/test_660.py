"""Testa a 6.6.0 contra uma Graph API simulada: anunciante verificado por ID (regional_regulation_identities) no
criar_conjunto e a ferramenta listar_anunciantes (com e sem a lista disponível).
Uso: python testes/test_660.py <raiz do repositório>"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx

sys.path.insert(0, sys.argv[1])
for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_EXPORT_DIR"):
    os.environ.pop(k, None)
import server  # noqa: E402

tmp = Path(tempfile.mkdtemp())
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR, server.HISTORICO_DIR = tmp, tmp / ".env", tmp / "clientes", tmp / "historico"
server._BACKOFF = (0, 0, 0)
(tmp / "clientes").mkdir()
TOKEN = "TOKEN_660_aa11"
ACC, ACC2 = "111111111111111", "333333333333333"
for nome, acc in (("c", ACC), ("d", ACC2)):
    (tmp / f"clientes/{nome}.env").write_text(f"NOME={nome}\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={acc}\n"
                                             "PERMISSOES=status,orcamento,editar,criar\n")
CAMP = "120000000000000002"
POSTS, URLS = [], []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    if req.method == "GET":
        if p in (f"act_{ACC}", f"act_{ACC2}"):
            return httpx.Response(200, json={"account_id": p[4:], "name": "CA 01", "currency": "BRL", "timezone_name": "America/Sao_Paulo"})
        if p == f"act_{ACC}/regional_regulation_identities":
            return httpx.Response(200, json={"data": [
                {"id": "810000000000001", "name": "PAULO RODRIGO DE TOLEDO 10142577600", "identity_type": "UNIVERSAL", "verification_status": "COMPLETED"},
                {"id": "810000000000002", "name": "SANDRO BISPO DOS SANTOS LTDA", "identity_type": "UNIVERSAL", "verification_status": "COMPLETED"}]})
        if p.startswith(f"act_{ACC2}/"):
            return err(400, 100, "(#100) Tried accessing nonexisting field")
        if p == f"act_{ACC}/adspixels":
            return httpx.Response(200, json={"data": [{"id": "777000000000001", "name": "Pixel"}]})
        if p == CAMP:
            return httpx.Response(200, json={"name": "[VENDAS]", "account_id": ACC, "objective": "OUTCOME_SALES"})
        return err(404, 100, f"GET sem mock: {p}")
    corpo = parse_qs(req.content.decode())
    POSTS.append((p, {k: v[0] for k, v in corpo.items()}))
    if p == f"act_{ACC}/adsets":
        ident = json.loads(corpo["regional_regulation_identities"][0]) if "regional_regulation_identities" in corpo else {}
        if ident.get("universal_beneficiary") not in ("810000000000001", "810000000000002"):
            return err(400, 100, "O anunciante está ausente: Forneça um anunciante verificado para que os anúncios neste conjunto sejam veiculados.")
        return httpx.Response(200, json={"id": "120000000000000888"})
    return err(404, 100, f"POST sem mock: {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


async def main():
    out = await server.listar_anunciantes(cliente="c")
    check("listar_anunciantes mostra os dois anunciantes verificados com ID", "810000000000001" in out and "SANDRO BISPO" in out and "ERRO" not in out, out)
    out = await server.listar_anunciantes(cliente="d")
    check("listar_anunciantes sem lista: explica o que fazer, sem estourar", "anunciante_id" in out and "Nenhuma identidade" in out, out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=20, evento="PURCHASE", pixel_id="777000000000001",
                                      dsa_beneficiario="PAULO RODRIGO DE TOLEDO 10142577600", cliente="c")
    check("só o nome em texto: a Meta recusa e o erro manda usar anunciante_id", "ERRO" in out and "listar_anunciantes" in out, out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=20, evento="PURCHASE", pixel_id="777000000000001",
                                      anunciante_id="810000000000001", cliente="c")
    d = [d for p, d in POSTS if p.endswith("/adsets")]
    ident = json.loads(d[0]["regional_regulation_identities"]) if d else {}
    check("anunciante_id: beneficiário e pagador vão como identidade verificada, conjunto nasce pausado",
          "ERRO" not in out and ident == {"universal_beneficiary": "810000000000001", "universal_payer": "810000000000001"}
          and d[0].get("status") == "PAUSED", out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=20, evento="PURCHASE", pixel_id="777000000000001",
                                      anunciante_id="810000000000001", pagador_id="810000000000002", cliente="c")
    d = [d for p, d in POSTS if p.endswith("/adsets")]
    ident = json.loads(d[0]["regional_regulation_identities"]) if d else {}
    check("pagador_id diferente do anunciante_id é respeitado", "ERRO" not in out and ident.get("universal_payer") == "810000000000002", out)

    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=20, evento="PURCHASE", pixel_id="777000000000001",
                                      campos_extras={"regional_regulation_identities": {"universal_beneficiary": "810000000000002"}}, cliente="c")
    check("campos_extras aceita regional_regulation_identities", "ERRO" not in out, out)
    check("o token nunca foi à URL", not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
