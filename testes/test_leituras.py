"""Testa as leituras da 6.1.0 (diagnóstico, log, prévia, criativos, Instagram, biblioteca, pixel, conversões, comparação) contra uma Graph API simulada.
Uso: python testes/test_leituras.py <raiz do repositório>"""
import asyncio, json, os, sys, tempfile
from datetime import date, timedelta
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
TOKEN = "TOKEN_LEITURAS_dd44"
ACC = "111111111111111"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=\n")
HOJE = date.today()
A1, A2 = HOJE - timedelta(days=7), HOJE - timedelta(days=1)
B1, B2 = HOJE - timedelta(days=15), HOJE - timedelta(days=9)
URLS, POSTS = [], []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"
    if req.method != "GET":
        POSTS.append(str(req.url)); return err(400, 100, "leituras não podem escrever")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    if p == f"act_{ACC}":
        return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL", "timezone_name": "America/Sao_Paulo"})
    if p == "100000000000001":
        return httpx.Response(200, json={"id": p, "account_id": ACC, "name": "ok", "metadata": {"type": "Ad"}})
    if p.endswith("/ads"):
        return httpx.Response(200, json={"data": [
            {"id": "1001", "name": "ok", "effective_status": "ACTIVE", "adset_id": "21", "campaign_id": "31"},
            {"id": "1002", "name": "reprovado", "effective_status": "DISAPPROVED", "adset_id": "21", "campaign_id": "31",
             "ad_review_feedback": {"global": {"Jogos de azar": "Política de apostas"}}},
            {"id": "1003", "name": "com erro", "effective_status": "WITH_ISSUES", "issues_info": [{"error_summary": "Imagem pequena"}]},
            {"id": "1004", "name": "pausado", "effective_status": "PAUSED"}]})
    if p.endswith("/adsets"):
        return httpx.Response(200, json={"data": [
            {"id": "21", "name": "aprendendo", "effective_status": "ACTIVE", "learning_stage_info": {"status": "LEARNING"}},
            {"id": "22", "name": "limitado", "effective_status": "ACTIVE", "learning_stage_info": {"status": "FAIL"}}]})
    if p.endswith("/activities"):
        return httpx.Response(200, json={"data": [{"event_time": "2026-10-20T10:00:00+0000", "translated_event_type": "Orçamento atualizado",
                                                  "actor_name": "Marcelo", "object_type": "AD_SET", "object_name": "aberto", "object_id": "21",
                                                  "extra_data": "{\"old\":5000,\"new\":8000}"}]})
    if p.endswith("/previews"):
        if q["ad_format"] == "INSTAGRAM_REELS":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"body": '<iframe src="https://www.facebook.com/ads/api/preview_iframe.php?d=abc&amp;t=xyz" width="320"></iframe>'}]})
    if p.endswith("/adcreatives"):
        return httpx.Response(200, json={"data": [{"id": "5001", "name": "criativo 1", "status": "ACTIVE", "object_type": "SHARE", "title": "Concorra",
                                                  "body": "Ganhe um carro", "call_to_action_type": "SHOP_NOW", "thumbnail_url": "https://t/1.jpg"}]})
    if p.endswith("/instagram_accounts"):
        return httpx.Response(200, json={"data": [{"id": "17841400000000001", "username": "primos"}]})
    if p == "17841400000000001/media":
        return httpx.Response(200, json={"data": [{"id": "179001", "caption": "Sorteio\nde hoje", "media_type": "VIDEO", "permalink": "https://ig/p/1",
                                                  "timestamp": "2026-10-21T10:00:00+0000", "like_count": 120, "comments_count": 9}]})
    if p == "ads_archive":
        assert json.loads(q["ad_reached_countries"]) == ["BR"] and q["search_terms"] == "rifa"
        return httpx.Response(200, json={"data": [{"id": "9001", "page_name": "Concorrente", "ad_creative_bodies": ["Compre sua rifa"],
                                                  "ad_delivery_start_time": "2026-09-01", "ad_snapshot_url": "https://fb/ads/archive/render_ad/?id=9001"}]})
    if p.endswith("/adspixels"):
        return httpx.Response(200, json={"data": [{"id": "777", "name": "Pixel A", "last_fired_time": "2026-10-23T12:00:00+0000"},
                                                  {"id": "778", "name": "Pixel B"}]})
    if p == "777/stats":
        assert q["aggregation"] == "event"
        return httpx.Response(200, json={"data": [{"start_time": "x", "data": [{"value": "PageView", "count": 900}, {"value": "Purchase", "count": 4}]},
                                                 {"start_time": "y", "data": [{"value": "PageView", "count": 100}]}]})
    if p == "778/stats":
        return httpx.Response(200, json={"data": [{"data": [{"value": "PageView", "count": 5}]}]})
    if p.endswith("/customconversions"):
        return httpx.Response(200, json={"data": [{"id": "8001", "name": "Obrigado", "custom_event_type": "PURCHASE", "rule": "{\"url\":{\"i_contains\":\"obrigado\"}}",
                                                  "is_archived": False, "pixel": {"id": "777", "name": "Pixel A"}}]})
    if p.endswith("/insights"):
        tr = json.loads(q["time_range"]); novo = tr["since"] == A1.isoformat()
        def linha(cid, nome, gasto, compras, valor):
            return {"account_id": ACC, "campaign_id": cid, "campaign_name": nome, "objective": "OUTCOME_SALES", "spend": str(gasto), "impressions": "1000",
                    "reach": "800", "frequency": "1.25", "inline_link_clicks": "50",
                    "actions": [{"action_type": "purchase", "value": str(compras)}], "action_values": [{"action_type": "purchase", "value": str(valor)}]}
        if novo:
            rows = [linha("31", "Vendas", 200, 10, 1000), linha("32", "Nova", 50, 1, 80)]
        else:
            rows = [linha("31", "Vendas", 100, 4, 400)]
        if q["level"] == "account":
            rows = [linha("", "", 250 if novo else 100, 11 if novo else 4, 1080 if novo else 400)]
            for r in rows: r.pop("campaign_id"); r.pop("campaign_name"); r.pop("objective")
        return httpx.Response(200, json={"data": rows})
    return err(400, 100, f"Unsupported get request {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


async def main():
    out = await server.diagnosticar_entrega(cliente="c")
    check("anúncios: só o que tem problema, com o motivo", "reprovado" in out and "Política de apostas" in out and "Imagem pequena" in out
          and ",ok," not in out and "pausado" not in out, out[:300])
    out = await server.diagnosticar_entrega(incluir_normais=True, cliente="c")
    check("incluir_normais mostra os sem problema", ",ok," in out and "pausado" in out)
    out = await server.diagnosticar_entrega(nivel="conjunto", cliente="c")
    check("conjuntos: aprendizado limitado aparece, aprendendo não é problema", "limitado" in out and "aprendizado limitado" in out and "aprendendo," not in out, out)
    out = await server.log_atividades(cliente="c")
    check("log de atividades: quem, o quê, objeto", "Marcelo" in out and "Orçamento atualizado" in out and "aberto" in out)
    out = await server.previa_anuncio(anuncio_id="100000000000001", cliente="c")
    check("prévia devolve o link sem entidades HTML", "preview_iframe.php?d=abc&t=xyz" in out and "&amp;" not in out, out[-120:])
    out = await server.previa_anuncio(anuncio_id="100000000000001", formato="instagram_reels", cliente="c")
    check("formato sem prévia: explica sem inventar link", "não devolveu prévia" in out)
    out = await server.listar_criativos(cliente="c")
    check("criativos listados", "criativo 1" in out and "SHOP_NOW" in out and "Ganhe um carro" in out)
    out = await server.listar_instagram(cliente="c")
    check("perfis do Instagram", "17841400000000001" in out and "primos" in out)
    out = await server.listar_instagram(instagram_id="17841400000000001", cliente="c")
    check("posts do Instagram (legenda numa linha só)", "179001" in out and "Sorteio de hoje" in out and "120" in out)
    out = await server.listar_instagram(instagram_id="17841499999999999", cliente="c")
    check("perfil que não é da conta: recusa", out.startswith("ERRO") and "não está ligado" in out)
    out = await server.biblioteca_anuncios(termo="rifa", cliente="c")
    check("biblioteca de anúncios", "Concorrente" in out and "Compre sua rifa" in out and "render_ad" in out)
    out = await server.biblioteca_anuncios(termo="rifa", paises=["brasil"], cliente="c")
    check("país inválido recusado", out.startswith("ERRO") and "2 letras" in out)
    out = await server.diagnosticar_pixel(cliente="c")
    check("pixel: eventos somados por tipo", "PageView,1000" in out and "Purchase,4" in out and "ATENÇÃO" not in out, out)
    out = await server.diagnosticar_pixel(pixel_id="778", cliente="c")
    check("pixel sem Purchase é apontado", "nenhum Purchase" in out, out)
    out = await server.diagnosticar_pixel(pixel_id="999", cliente="c")
    check("pixel de outra conta: recusa", out.startswith("ERRO") and "não é da conta padrão" in out)
    out = await server.listar_conversoes_personalizadas(cliente="c")
    check("conversões personalizadas", "Obrigado" in out and "PURCHASE" in out and "Pixel A" in out)
    out = await server.comparar_periodos(desde_a=A1.isoformat(), ate_a=A2.isoformat(), desde_b=B1.isoformat(), ate_b=B2.isoformat(), cliente="c")
    linhas = {l.split(",")[1]: l.split(",") for l in out.splitlines() if l.startswith(("Vendas", "Nova"))}
    cab = next(l for l in out.splitlines() if l.startswith("campanha,id")).split(",")
    v = linhas["31"]
    check("comparação: gasto A=200, B=100, variação +100%", v[cab.index("gasto_A")] == "200.0" or v[cab.index("gasto_A")] == "200" ,
          v[cab.index("gasto_A")])
    check("comparação: variação de gasto +100%", v[cab.index("gasto_var_pct")] in ("100.0", "100"), v[cab.index("gasto_var_pct")])
    check("comparação: campanha só no período A fica sem B e sem variação", linhas["32"][cab.index("gasto_B")] == "" and linhas["32"][cab.index("gasto_var_pct")] == "")
    out = await server.comparar_periodos(desde_a=A1.isoformat(), ate_a=A2.isoformat(), desde_b=B1.isoformat(), ate_b=B2.isoformat(), nivel="conta", cliente="c")
    check("comparação no nível da conta", "conta,conta" in out, out[-200:])
    check("nada foi escrito e o token nunca foi à URL", not POSTS and not any(TOKEN in u for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
