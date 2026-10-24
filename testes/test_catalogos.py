"""Testa a 6.2.0 (conversão personalizada, catálogos, testes A/B) contra uma Graph API simulada.
Uso: python testes/test_catalogos.py <raiz do repositório>"""
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
TOKEN = "TOKEN_CATALOGO_ee55"
ACC = "111111111111111"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=criar\n")
(tmp / "clientes/leitor.env").write_text(f"NOME=leitor\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=\n")
POSTS, URLS = [], []
ESTADO = {"diag_falha": False}


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    if req.method == "POST":
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        POSTS.append((p, form)); return httpx.Response(200, json={"id": "8002"})
    if p == f"act_{ACC}":
        return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL"})
    if p.endswith("/product_catalogs"):
        return httpx.Response(200, json={"data": [{"id": "4001", "name": "Loja", "product_count": 120, "vertical": "commerce"}]})
    if p.endswith("/adspixels"):
        return httpx.Response(200, json={"data": [{"id": "777", "name": "Pixel A"}]})
    if p.endswith("/customconversions"):
        return httpx.Response(200, json={"data": [{"id": "8001", "name": "Existente"}]})
    if p == "4001/product_sets":
        return httpx.Response(200, json={"data": [{"id": "6001", "name": "Todos", "product_count": 120, "filter": {"price": {"gt": 1000}}}]})
    if p == "4001/products":
        assert json.loads(q["filter"]) == {"name": {"i_contains": "tenis"}} if "filter" in q else True
        return httpx.Response(200, json={"data": [{"id": "9001", "retailer_id": "SKU1", "name": "Tênis", "price": "R$ 199,90", "availability": "in stock",
                                                  "review_status": "approved", "url": "https://loja/tenis"}]})
    if p == "4001/product_feeds":
        return httpx.Response(200, json={"data": [{"id": "5001", "name": "Feed diário", "latest_upload": {
            "end_time": "2026-10-23T08:00:00+0000", "num_detected_items": 120, "num_persisted_items": 110, "num_invalid_items": 10, "error_count": 2, "warning_count": 0}}]})
    if p == "4001/diagnostics":
        if ESTADO["diag_falha"]:
            return err(400, 100, "Tried accessing nonexisting field (title)")
        return httpx.Response(200, json={"data": [{"severity": "MUST_FIX", "title": "Imagem ausente", "number_of_affected_items": 10}]})
    if p.endswith("/ad_studies"):
        return httpx.Response(200, json={"data": [{"id": "3001", "name": "Teste criativo", "type": "SPLIT_TEST", "start_time": "2026-10-01T00:00:00+0000",
                                                  "end_time": "2026-10-10T00:00:00+0000",
                                                  "cells": {"data": [{"name": "A", "treatment_percentage": 50}, {"name": "B", "treatment_percentage": 50}]}}]})
    return err(400, 100, f"Unsupported get request {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


async def main():
    n = len(POSTS)
    out = await server.criar_conversao_personalizada(nome="x", url_regra="obrigado", cliente="leitor")
    check("sem permissão: recusa sem enviar nada", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.criar_conversao_personalizada(nome="x", url_regra="obrigado", evento="COMPRA", cliente="c")
    check("evento inválido: recusa", out.startswith("ERRO") and "Evento inválido" in out and len(POSTS) == n)
    out = await server.criar_conversao_personalizada(nome="existente", url_regra="obrigado", cliente="c")
    check("nome repetido: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.criar_conversao_personalizada(nome="x", url_regra="obrigado", pixel_id="999", cliente="c")
    check("pixel de outra conta: recusa", out.startswith("ERRO") and "não é da conta padrão" in out and len(POSTS) == n)
    out = await server.criar_conversao_personalizada(nome="Obrigado - Compra", url_regra="obrigado", valor_padrao=50, cliente="c")
    p, f = POSTS[-1]
    check("conversão criada no pixel da conta com regra de URL", p == f"act_{ACC}/customconversions" and f["event_source_id"] == "777" and f["custom_event_type"] == "PURCHASE"
          and json.loads(f["rule"]) == {"and": [{"url": {"i_contains": "obrigado"}}]} and f["default_conversion_value"] in ("50", "50.0") and "8002" in out, str(f))
    h = [json.loads(l) for l in (tmp / "historico/c.jsonl").read_text().splitlines()]
    check("fica no histórico, sem token", h and h[-1]["ferramenta"] == "criar_conversao_personalizada" and TOKEN not in (tmp / "historico/c.jsonl").read_text())

    out = await server.listar_catalogos(cliente="c")
    check("catálogos listados", "Loja" in out and "120" in out)
    out = await server.listar_conjuntos_produtos(catalogo_id="4001", cliente="c")
    check("conjuntos de produtos com filtro", "Todos" in out and "gt" in out)
    out = await server.listar_conjuntos_produtos(catalogo_id="4999", cliente="c")
    check("catálogo de outra conta: recusa", out.startswith("ERRO") and "não está ligado" in out)
    out = await server.listar_produtos(catalogo_id="4001", cliente="c")
    check("produtos com preço e aprovação", "SKU1" in out and "199,90" in out and "approved" in out)
    out = await server.listar_produtos(catalogo_id="4001", busca="tenis", cliente="c")
    check("busca por nome aceita", "SKU1" in out)
    out = await server.diagnosticar_catalogo(catalogo_id="4001", cliente="c")
    check("diagnóstico: feed com inválidos gera atenção e lista problemas", "ATENÇÃO" in out and "Feed diário" in out and "Imagem ausente" in out and "MUST_FIX" in out, out)
    ESTADO["diag_falha"] = True
    out = await server.diagnosticar_catalogo(catalogo_id="4001", cliente="c")
    check("se o diagnóstico de itens falha, ainda mostra os feeds e avisa", not out.startswith("ERRO") and "Feed diário" in out and "indisponível" in out)
    out = await server.listar_testes_ab(cliente="c")
    check("testes A/B com células", "Teste criativo" in out and "A (50%)" in out and "SPLIT_TEST" in out)
    check("token nunca foi à URL", not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
