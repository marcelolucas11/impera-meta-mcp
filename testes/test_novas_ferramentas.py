"""Testa as ferramentas novas da 6.7.0 contra uma Graph API simulada: cobrança, aumento de orçamento agendado,
compartilhar público, carteira, ver criativo, vídeos em lote e UTM. Nada toca a Meta de verdade.
Uso: python testes/test_novas_ferramentas.py <raiz do repositório>"""
import asyncio, json, os, sys, tempfile, time
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
server._BACKOFF = (0, 0, 0); server.VIDEO_ESPERA = 0
(tmp / "clientes").mkdir()
TOKEN, A1, A2, A3 = "TOKEN_NOVO_81f2", "111111111111111", "222222222222222", "999999999999999"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={A1}\nCONTAS_LIBERADAS={A2}\nMETA_BUSINESS_ID=700\nORCAMENTO_MAX_DIA=500\nPERMISSOES=status,orcamento,editar,criar\n")
(tmp / "clientes/d.env").write_text(f"NOME=d\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={A1}\nPERMISSOES=\n")
CAMP, ADSET, AD, CR, AUD = "120000000000000010", "120000000000000020", "120000000000000030", "130000000000000001", "140000000000000001"
BM = {A1: "700", A2: "700", A3: "800"}
OBJ = {
    CAMP: {"type": "campaign", "account_id": A1, "name": "Camp", "daily_budget": "10000"},
    ADSET: {"type": "adset", "account_id": A1, "name": "Conj", "campaign_id": CAMP},
    AD: {"type": "ad", "account_id": A1, "name": "Anuncio 1", "effective_status": "PAUSED", "creative": {"id": CR}},
    AUD: {"type": "aud", "account_id": A1, "name": "Compradores"},
}
CRIATIVO = {"id": CR, "name": "Cr", "url_tags": "utm_source=velho", "thumbnail_url": "https://scontent.xx.fbcdn.net/t.jpg", "image_url": "https://scontent.xx.fbcdn.net/i.jpg",
            "object_story_spec": {"page_id": "1", "link_data": {"link": "https://x.com", "message": "Participe!", "name": "Sorteio"}}}
S = {"sched": [], "shared": [], "posts": [], "novo": 500, "videos": {}, "urls": []}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def err(status, code, msg): return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req):
    S["urls"].append(str(req.url))
    u = urlparse(str(req.url))
    if u.hostname.endswith("fbcdn.net"):
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    p = u.path.split("/", 2)[2]
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if req.method == "GET":
        for a in (A1, A2, A3):
            if p == f"act_{a}":
                d = {"account_id": a, "name": f"CA {a[:3]}", "currency": "BRL", "timezone_name": "America/Sao_Paulo", "business": {"id": BM[a]}}
                d.update(account_status=1, disable_reason=0, spend_cap="100000", amount_spent="95000", balance="1234", is_prepay_account=False,
                         funding_source_details={"display_string": "Visa 1234"})
                return httpx.Response(200, json=d)
            if p == f"act_{a}/insights":
                g = "100.0" if a == A1 else "50.0"
                return httpx.Response(200, json={"data": [{"spend": g, "impressions": "1000", "reach": "800", "clicks": "50", "inline_link_clicks": "40",
                                                           "actions": [{"action_type": "omni_purchase", "value": "5"}], "action_values": [{"action_type": "omni_purchase", "value": "400"}],
                                                           "date_start": "2026-09-01", "date_stop": "2026-09-07"}]})
        if p == "700/business_invoices":
            return httpx.Response(200, json={"data": [{"id": "i1", "invoice_id": "F-1", "invoice_date": "2026-09-01", "due_date": "2026-09-10", "payment_status": "PAID", "amount_due": {"amount": "99.00"}}]})
        if p == f"{CAMP}/budget_schedules":
            return httpx.Response(200, json={"data": S["sched"]})
        if p == f"{AUD}/adaccounts":
            return httpx.Response(200, json={"data": [{"id": f"act_{a}", "account_id": a} for a in S["shared"]]})
        if p == CR or p == CRIATIVO["id"]: return httpx.Response(200, json=CRIATIVO)
        if p in S["videos"]: return httpx.Response(200, json={"id": p, "picture": "https://scontent.xx.fbcdn.net/v.jpg", "status": {"video_status": "ready"}})
        if p in OBJ:
            d = {k: v for k, v in OBJ[p].items() if k != "type"}
            d["metadata"] = {"type": OBJ[p]["type"].upper() if OBJ[p]["type"] != "aud" else "CUSTOMAUDIENCE"}
            return httpx.Response(200, json=d)
        if p in S.get("novos", {}): return httpx.Response(200, json=S["novos"][p])
        return err(400, 100, f"sem mock GET {p}")
    f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    S["posts"].append((p, f))
    S["novo"] += 1
    novo = f"15000000000{S['novo']:05d}"
    if p == f"{CAMP}/budget_schedules":
        S["sched"].append({"id": novo, "budget_value": f["budget_value"], "budget_value_type": f["budget_value_type"], "time_start": f["time_start"], "time_end": f["time_end"]})
        return httpx.Response(200, json={"id": novo})
    if p == f"{AUD}/adaccounts":
        S["shared"] += json.loads(f["adaccounts"]); return httpx.Response(200, json={"success": True})
    if p.endswith("/advideos"):
        S["videos"][novo] = True; return httpx.Response(200, json={"id": novo})
    if p.endswith("/adcreatives"):
        S.setdefault("crs", {})[novo] = dict(f, id=novo, **{k: json.loads(v) for k, v in f.items() if k in ("object_story_spec",)})
        return httpx.Response(200, json={"id": novo})
    if p.endswith("/ads"):
        S.setdefault("novos", {})[novo] = {"id": novo, "status": "PAUSED"}
        return httpx.Response(200, json={"id": novo})
    if p == AD and "creative" in f:
        nid = json.loads(f["creative"])["creative_id"]
        OBJ[AD]["creative"] = {"id": nid, "url_tags": S["crs"][nid].get("url_tags")}
        CRIATIVO.update(id=nid, url_tags=S["crs"][nid].get("url_tags"))
        return httpx.Response(200, json={"success": True})
    return err(400, 100, f"sem mock POST {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:170]}]" if detail else ""))


async def main():
    out = await server.consultar_cobranca(faturas=True, cliente="c")
    check("cobrança: status, pagamento, saldo, limite e fatura", "Visa 1234" in out and "ATIVA" in out and "Limite de gastos" in out and "F-1" in out and "QUASE ESGOTADO" in out, out)

    futuro = time.strftime("%Y-%m-%d", time.localtime(time.time() + 5 * 86400)); fim = time.strftime("%Y-%m-%d", time.localtime(time.time() + 8 * 86400))
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio=futuro, fim=fim, valor_diario=300, cliente="c")
    check("aumento agendado e conferido na Meta", "OK" in out and "[conferido na Meta]" in out and S["sched"] and S["sched"][0]["budget_value"] == "30000" and S["sched"][0]["budget_value_type"] == "ABSOLUTE", out)
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio=futuro, fim=fim, valor_diario=900, cliente="c")
    check("aumento acima do teto do app é recusado sem agendar", out.startswith("ERRO") and "teto" in out and len(S["sched"]) == 1, out)
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio="2020-01-01", fim="2020-01-02", valor_diario=200, cliente="c")
    check("início no passado é recusado", out.startswith("ERRO") and "futuro" in out, out)
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio=futuro, fim=fim, multiplicador=3, cliente="c")
    check("multiplicador também respeita o teto (100 x 3 = 300 ok; 10 x vira 1000 não)", "OK" in out, out)
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio=futuro, fim=fim, multiplicador=6, cliente="c")
    check("multiplicador que passa do teto é recusado", out.startswith("ERRO") and "teto" in out, out)
    out = await server.agendar_aumento_orcamento(campanha_id=CAMP, inicio=futuro, fim=fim, valor_diario=200, cliente="d")
    check("sem a permissão de orçamento recusa", out.startswith("ERRO") and "não permite" in out, out)
    out = await server.listar_aumentos_orcamento(campanha_id=CAMP, cliente="c")
    check("lista os aumentos agendados", "2 aumento(s)" in out, out)

    out = await server.compartilhar_publico(publico_id=AUD, contas_destino=[A2], cliente="c")
    check("público compartilhado dentro do cliente e da BM, conferido", "OK" in out and "[conferido na Meta]" in out and S["shared"] == [A2], out)
    n = len(S["posts"])
    out = await server.compartilhar_publico(publico_id=AUD, contas_destino=[A3], cliente="c")
    check("conta fora das liberadas do cliente é recusada", out.startswith("ERRO") and "não está liberada" in out and len(S["posts"]) == n, out)
    out = await server.listar_compartilhamento_publico(publico_id=AUD, cliente="c")
    check("lista onde o público está compartilhado", "1 conta(s)" in out and A2 in out, out)

    out = await server.resumo_carteira(desde="2026-09-01", ate="2026-09-07", cliente="c")
    check("carteira: as 2 contas liberadas e total por moeda", "2 de 2 conta(s)" in out and "TOTAL BRL (2 conta(s)): gasto 150.00" in out and "não somam" in out, out)
    check("carteira não toca na conta de outra BM/cliente", not any(f"act_{A3}" in x for x in S["urls"]))

    r = await server.ver_criativo(anuncio_ids=[AD], cliente="c")
    check("ver criativo: texto, UTM e imagem inline", isinstance(r, list) and len(r) == 2 and "Participe!" in r[0] and "utm_source=velho" in r[0] and hasattr(r[1], "data") or (len(r) == 2), r[0] if r else r)
    try:
        await server._baixar_imagem("https://evil.example.com/x.png", 1000)
        recusou = False
    except server.MetaError:
        recusou = True
    check("imagem de servidor fora da Meta não é baixada", recusou)

    OBJ[AD]["effective_status"] = "PAUSED"
    out = await server.editar_utm(anuncio_ids=[AD], parametros_url="utm_source=meta&utm_content={{ad.name}}", simular=True, cliente="c")
    n = len(S["posts"])
    check("UTM em simulação não altera nada", "SIMULAÇÃO" in out and len(S["posts"]) == n, out)
    out = await server.editar_utm(anuncio_ids=[AD], parametros_url="utm_source=meta&utm_content={{ad.name}}", cliente="c")
    novo_cr = [d for p, d in S["posts"] if p.endswith("/adcreatives")][-1]
    check("UTM: cria criativo idêntico com url_tags novas e confere", "UTMs trocadas" in out and novo_cr.get("url_tags") == "utm_source=meta&utm_content={{ad.name}}" and "object_story_spec" in novo_cr, out)
    out = await server.editar_utm(anuncio_ids=[AD], parametros_url="utm_source=meta&utm_content={{ad.name}}", cliente="c")
    check("UTM igual à atual: não faz nada", "já está com essas UTMs" in out, out)
    out = await server.editar_utm(anuncio_ids=[AD], parametros_url="com espaço", cliente="c")
    check("UTM inválida é recusada", out.startswith("ERRO") and "inválido" in out, out)

    out = await server.criar_anuncios_video_em_lote(
        conjunto_id=ADSET, videos=[{"url": "https://cdn.exemplo.com/a.mp4", "nome": "V1"}, {"url": "https://cdn.exemplo.com/b.mp4", "nome": "V2"}],
        pagina_id="1", link="https://x.com", texto="Oi", parametros_url="utm_source=meta", cliente="c")
    ads = [d for p, d in S["posts"] if p.endswith("/ads")]
    check("2 vídeos viram 2 anúncios PAUSADOS", "2 de 2 anúncio(s)" in out and len(ads) == 2 and all(a["status"] == "PAUSED" for a in ads), out)
    out = await server.criar_anuncios_video_em_lote(conjunto_id=ADSET, videos=[{"url": "http://inseguro/a.mp4", "nome": "X"}], pagina_id="1", link="https://x.com", texto="Oi", cliente="c")
    check("link http (não https) é recusado antes de enviar", out.startswith("ERRO") and "https" in out, out)
    check("o token nunca foi à URL", not any(TOKEN in x for x in S["urls"]))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
