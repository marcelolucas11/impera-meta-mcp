"""Testa as correções da 6.5.1 contra uma Graph API simulada: data do público como número, catálogos pela BM,
criação de público sem 'subtype' quando a Meta recusa, DSA e campos_extras como objeto.
Uso: python testes/test_651.py <raiz do repositório>"""
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
TOKEN = "TOKEN_651_ee55"
ACC = "111111111111111"
BIZ = "222222222222222"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nMETA_BUSINESS_ID={BIZ}\n"
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
        if p == f"act_{ACC}":
            return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL", "timezone_name": "America/Sao_Paulo"})
        if p == f"act_{ACC}/customaudiences":
            return httpx.Response(200, json={"data": [{"id": "900000000000001", "name": "Compradores", "subtype": "CUSTOM",
                                                       "time_created": 1790000000}]})
        if p == f"act_{ACC}/product_catalogs":
            return err(400, 100, "(#100) Tried accessing nonexisting field (product_catalogs)")
        if p == f"{BIZ}/owned_product_catalogs":
            return httpx.Response(200, json={"data": [{"id": "500000000000001", "name": "Catálogo próprio", "product_count": 10, "vertical": "commerce"}]})
        if p == f"{BIZ}/client_product_catalogs":
            return httpx.Response(200, json={"data": [{"id": "500000000000002", "name": "Catálogo compartilhado", "product_count": 3, "vertical": "commerce"}]})
        if p == f"act_{ACC}/adspixels":
            return httpx.Response(200, json={"data": [{"id": "777000000000001", "name": "Pixel"}]})
        if p == CAMP:
            return httpx.Response(200, json={"name": "[TRAFEGO] ABO", "account_id": ACC, "objective": "OUTCOME_TRAFFIC"})
        return err(404, 100, f"GET sem mock: {p}")
    corpo = parse_qs(req.content.decode())
    POSTS.append((p, {k: v[0] for k, v in corpo.items()}))
    if p == f"act_{ACC}/customaudiences":
        if "subtype" in corpo:
            return err(400, 2654, 'Parâmetro inválido: O parâmetro "subtipo" não é aceito na versão atual da API.')
        return httpx.Response(200, json={"id": "900000000000009"})
    if p == f"act_{ACC}/adsets":
        if "dsa_beneficiary" not in corpo:
            return err(400, 100, "O anunciante está ausente: Forneça um anunciante verificado para que os anúncios neste conjunto de anúncios sejam veiculados.")
        return httpx.Response(200, json={"id": "120000000000000777"})
    return err(404, 100, f"POST sem mock: {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


async def main():
    out = await server.listar_publicos(cliente="c")
    check("listar_publicos aceita data como número (unix)", "Compradores" in out and "2026-" in out and "ERRO" not in out, out)
    out = await server.listar_catalogos(cliente="c")
    check("listar_catalogos: sem product_catalogs na conta, lê os da BM (próprios e compartilhados)",
          "Catálogo próprio" in out and "Catálogo compartilhado" in out and "ERRO" not in out, out)

    POSTS.clear()
    out = await server.criar_publico_site(nome="Todos 30d", pixel_id="777000000000001", cliente="c")
    posts = [d for p, d in POSTS if p.endswith("/customaudiences")]
    check("criar_publico_site: Meta recusa 'subtype' (2654), repete uma vez sem ele e cria",
          "OK público de site" in out and len(posts) == 2 and "subtype" in posts[0] and "subtype" not in posts[1], out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=10, meta_otimizacao="LINK_CLICKS", cliente="c")
    check("criar_conjunto sem DSA: erro da Meta vem com a dica de dsa_beneficiario", "dsa_beneficiario" in out and "ERRO" in out, out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=10, meta_otimizacao="LINK_CLICKS",
                                      dsa_beneficiario="Impera Services", cliente="c")
    d = [d for p, d in POSTS if p.endswith("/adsets")]
    check("criar_conjunto com dsa_beneficiario: beneficiário e pagador vão ao conjunto, nasce pausado",
          "ERRO" not in out and d and d[0].get("dsa_beneficiary") == "Impera Services" and d[0].get("dsa_payor") == "Impera Services"
          and d[0].get("status") == "PAUSED", out)

    POSTS.clear()
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=10, meta_otimizacao="LINK_CLICKS",
                                      campos_extras={"dsa_beneficiary": "A", "dsa_payor": "B"}, cliente="c")
    d = [d for p, d in POSTS if p.endswith("/adsets")]
    check("campos_extras aceita objeto (como o Claude Desktop entrega)", "ERRO" not in out and d and d[0].get("dsa_payor") == "B", out)
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=10, meta_otimizacao="LINK_CLICKS",
                                      campos_extras={"daily_budget": 99999}, cliente="c")
    check("campos_extras objeto continua só com campos permitidos", "não permitidos" in out, out)
    out = await server.criar_conjunto(campanha_id=CAMP, nome="s", orcamento_diario=10, meta_otimizacao="LINK_CLICKS",
                                      campos_extras='{"dsa_beneficiary": "C"}', cliente="c")
    check("campos_extras como texto JSON continua funcionando", "ERRO" not in out, out)
    check("o token nunca foi à URL", not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
