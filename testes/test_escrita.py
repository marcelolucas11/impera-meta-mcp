"""Testa as ferramentas de alteração contra uma Graph API simulada.

Uso: python testes/test_escrita.py <raiz do repositório>
Nenhuma chamada real à Meta. Confere permissões por cliente, conta padrão, teto de orçamento,
tudo criado PAUSADO, formato do que é enviado, histórico e que nada é repetido às cegas.
"""
import asyncio, base64, json, os, sys, tempfile
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
TOK = {"primos": "TOKEN_ESCRITA_PRIMOS_aa11", "dona": "TOKEN_ESCRITA_DONA_bb22"}


def cliente(nome, permissoes="", teto="", conta="111111111111111"):
    (tmp / f"clientes/{nome}.env").write_text(
        f"NOME={nome}\nMETA_ACCESS_TOKEN={TOK[nome]}\nMETA_AD_ACCOUNT_ID={conta}\nPERMISSOES={permissoes}\nORCAMENTO_MAX_DIA={teto}\n")


cliente("primos")
cliente("dona", "status,orcamento,editar,criar", conta="222222222222222")

OBJ = {  # objetos na Meta simulada
    "120000000000000001": {"name": "[VENDAS] cpa_600 aberto", "account_id": "111111111111111", "type": "campaign", "objective": "OUTCOME_SALES",
                           "daily_budget": "50000", "effective_status": "ACTIVE", "bid_strategy": "LOWEST_COST_WITHOUT_CAP"},
    "120000000000000002": {"name": "[VENDAS] sem orçamento", "account_id": "111111111111111", "type": "campaign", "objective": "OUTCOME_SALES",
                           "effective_status": "PAUSED"},
    "120000000000000011": {"name": "aberto 18-65", "account_id": "111111111111111", "type": "adset", "daily_budget": "20000",
                           "effective_status": "ACTIVE", "campaign_id": "120000000000000002"},
    "120000000000000021": {"name": "video_carro_01", "account_id": "111111111111111", "type": "ad", "effective_status": "ACTIVE"},
    "120000000000000003": {"name": "imp_vend_210826_cpa_teste_7", "account_id": "111111111111111", "type": "campaign",
                           "objective": "OUTCOME_SALES", "daily_budget": "600000", "effective_status": "ACTIVE"},
    "130000000000000001": {"name": "campanha de outro cliente", "account_id": "999999999999999", "type": "campaign", "effective_status": "ACTIVE"},
}
# Campos que só existem em certos tipos (a Meta real dá erro #100 para os outros).
SO_EM = {"adset_id": ("ad",), "campaign_id": ("ad", "adset"), "objective": ("campaign",)}
NO_TYPE = {"campaign": "Campaign", "adset": "AdSet", "ad": "Ad"}
POSTS, URLS, AUTH_IMAGEM, ESTADO = [], [], [], {"metadata": "normal", "falha_orcamento": False, "falha_500": False, "limite": 0, "timeout": False, "novo": 500}


VEICULOU = {
    "campaign": [{"campaign_id": "120000000000000001", "spend": "300.00", "impressions": "9000"},
                 {"campaign_id": "120000000000000002", "spend": "120.00", "impressions": "5000"},
                 {"campaign_id": "120000000000000099", "spend": "10.00", "impressions": "400"}],
    "adset": [{"campaign_id": "120000000000000002", "adset_id": "120000000000000011", "spend": "120.00", "impressions": "5000"}],
    "ad": [{"campaign_id": "120000000000000002", "adset_id": "120000000000000011", "ad_id": "120000000000000021",
            "spend": "120.00", "impressions": "5000"}],
}
TIPO_EDGE = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}
FILTROS = []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    if req.url.host == "imagens.exemplo":
        AUTH_IMAGEM.append(req.headers.get("authorization"))
        return httpx.Response(200, content=b"\x89PNG-imagem-de-teste", headers={"content-type": "image/png"})
    tok = req.headers.get("authorization", "").removeprefix("Bearer ")
    if tok not in TOK.values():
        return err(400, 190, "Invalid OAuth access token")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = parse_qs(urlparse(str(req.url)).query)
    if req.method == "GET":
        if p in ("act_111111111111111", "act_222222222222222"):
            return httpx.Response(200, json={"account_id": p[4:], "name": "CA 01 PRIMOS" if "111" in p else "DONA CA 02", "currency": "BRL"})
        if p.endswith("/adspixels"):
            return httpx.Response(200, json={"data": [{"id": "777000000000001", "name": "Pixel Primos", "last_fired_time": "2026-10-15T10:00:00+0000"}]})
        if p.endswith("/promote_pages"):
            return httpx.Response(200, json={"data": [{"id": "888000000000001", "name": "Sorteio dos Primos", "instagram_business_account": {"id": "17841400000000001", "username": "sorteiodosprimos"}}]})
        if p.endswith("/adimages"):
            return httpx.Response(200, json={"data": [{"hash": "abc123hash", "name": "carro.jpg", "width": 1080, "height": 1080, "created_time": "2026-10-01T00:00:00+0000"}]})
        if p.endswith("/insights"):
            return httpx.Response(200, json={"data": [dict(r, account_id="111111111111111", date_start=q["time_range"][0][10:20],
                                                           date_stop="x") for r in VEICULOU[q["level"][0]]]})
        if p.split("/")[-1] in TIPO_EDGE:
            dono, edge = p.split("/")[0], p.split("/")[-1]
            itens = [dict(o, id=k) for k, o in OBJ.items() if o["type"] == TIPO_EDGE[edge] and o["account_id"] == "111111111111111"]
            if dono in OBJ:  # conjuntos de uma campanha
                itens = [i for i in itens if i.get("campaign_id") == dono]
            if q.get("effective_status"):
                itens = [i for i in itens if i["effective_status"] in json.loads(q["effective_status"][0])]
            if q.get("filtering"):
                fl = json.loads(q["filtering"][0]); FILTROS.append((edge, fl))
                ids = [v for f in fl if f["field"] == "id" for v in f["value"]]
                itens = [i for i in itens if i["id"] in ids]
            return httpx.Response(200, json={"data": itens})
        if p == "search":
            return httpx.Response(200, json={"data": [{"key": "260131", "name": "Feira de Santana", "region": "Bahia", "country_code": "BR"}]})
        if p in OBJ:
            o = OBJ[p]
            campos = q.get("fields", [""])[0].split(",")
            for k in campos:
                if k in SO_EM and o["type"] not in SO_EM[k]:
                    return err(400, 100, f"(#100) Tried accessing nonexisting field ({k}) on node type ({NO_TYPE[o['type']]})")
            body = {"id": p, **{k: o[k] for k in campos if k in o}}
            if "adset_id" in campos and o["type"] == "ad":
                body["adset_id"] = "120000000000000011"
            if q.get("metadata") and ESTADO["metadata"] != "sem":
                legado = {"campaign": "adcampaign_group", "adset": "adcampaign", "ad": "adgroup"}
                body["metadata"] = {"type": legado[o["type"]] if ESTADO["metadata"] == "legado" else o["type"]}
            return httpx.Response(200, json=body)
        if p == "555000000000001":
            return httpx.Response(200, json={"picture": "https://scontent.exemplo/thumb.jpg", "id": p})
        return err(400, 100, f"Unsupported get request {p}")
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert not any(t in req.content.decode() for t in TOK.values()), "token no corpo do POST"
    if ESTADO["timeout"]:
        ESTADO["timeout"] = False; POSTS.append((p, form, "timeout"))
        raise httpx.ReadTimeout("simulado", request=req)
    if ESTADO["limite"] > 0:
        ESTADO["limite"] -= 1; POSTS.append((p, form, "limite"))
        return err(400, 17, "User request limit reached")
    if ESTADO["falha_500"]:
        ESTADO["falha_500"] = False; POSTS.append((p, form, "500"))
        return err(500, 2, "Service temporarily unavailable")
    if ESTADO["falha_orcamento"] and "daily_budget" in form:
        POSTS.append((p, form, "recusado")); return err(400, 100, "Invalid parameter: daily_budget")
    POSTS.append((p, form, "ok"))
    ESTADO["novo"] += 1
    novo = f"120000000000{ESTADO['novo']:06d}"
    if p.endswith("/copies"):
        tipo = OBJ[p.split("/")[0]]["type"]
        chave = {"campaign": "copied_campaign_id", "adset": "copied_adset_id", "ad": "copied_ad_id"}[tipo]
        return httpx.Response(200, json={chave: novo, "ad_object_ids": [{"source_id": "x", "copied_id": "y"}] if tipo != "ad" else []})
    if p.endswith("/adimages"):
        return httpx.Response(200, json={"images": {form.get("name", "img"): {"hash": "hashnovo999", "url": "https://x"}}})
    if p.endswith(("/campaigns", "/adsets", "/ads", "/adcreatives")):
        return httpx.Response(200, json={"id": novo})
    return httpx.Response(200, json={"success": True})


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def posts_desde(n):
    return POSTS[n:]


def hist(nome):
    arq = tmp / f"historico/{nome}.jsonl"
    return [json.loads(l) for l in arq.read_text().splitlines()] if arq.exists() else []


async def main():
    n = len(POSTS)
    out = await server.alterar_status(ids=["120000000000000001"], acao="pausar", cliente="primos")
    check("sem permissão: recusa e explica onde liberar", out.startswith("ERRO") and "não permite ativar e pausar" in out and "O que o Claude pode fazer" in out, out[:110])
    check("sem permissão: nada enviado à Meta", posts_desde(n) == [])

    cliente("primos", "status,orcamento,editar,criar", teto="500")
    n = len(POSTS)
    out = await server.alterar_status(ids=["120000000000000001", "120000000000000021"], acao="ativar", cliente="primos")
    enviados = posts_desde(n)
    check("ativar: um POST por objeto com status=ACTIVE", [(p, f.get("status")) for p, f, _ in enviados] ==
          [("120000000000000001", "ACTIVE"), ("120000000000000021", "ACTIVE")], out.replace("\n", " | ")[:160])
    check("ativar: histórico gravado por cliente, permissão 600", len(hist("primos")) == 2 and all(h["ok"] for h in hist("primos"))
          and oct((tmp / "historico/primos.jsonl").stat().st_mode & 0o777) == "0o600")

    n = len(POSTS)
    out = await server.alterar_status(ids=["130000000000000001"], acao="pausar", cliente="primos")
    check("objeto de outra conta: recusado sem enviar nada", "ERRO" in out and "não da conta padrão" in out and posts_desde(n) == [], out[:120])

    n = len(POSTS)
    out = await server.alterar_orcamento(id="120000000000000001", orcamento_diario=600, cliente="primos")
    check("teto: R$ 600 com teto de R$ 500 recusado", out.startswith("ERRO") and "passa do teto" in out and posts_desde(n) == [], out[:100])
    out = await server.alterar_orcamento(id="120000000000000001", orcamento_diario=350.5, cliente="primos")
    p, f, _ = POSTS[-1]
    check("orçamento: R$ 350,50 vira 35050 centavos", p == "120000000000000001" and f == {"daily_budget": "35050"} and "500.00 -> 350.50" in out, out.splitlines()[-1][:120])
    out = await server.alterar_orcamento(id="120000000000000001", limite_cpa=6, cliente="primos")
    check("limite de CPA em campanha: recusado", out.startswith("ERRO") and "conjunto" in out)
    await server.alterar_orcamento(id="120000000000000011", limite_cpa=6.5, roas_minimo=2.2, cliente="primos")
    p, f, _ = POSTS[-1]
    check("limite de CPA e ROAS mínimo no conjunto", f.get("bid_amount") == "650" and json.loads(f["bid_constraints"]) == {"roas_average_floor": 22000})
    check("moeda sem centavos (CLP) não multiplica por 100", server._centavos(15000, "CLP") == 15000 and server._centavos(15, "BRL") == 1500)

    await server.renomear(id="120000000000000021", nome="video_carro_01 - vencedor", cliente="primos")
    check("renomear", POSTS[-1][:2] == ("120000000000000021", {"name": "video_carro_01 - vencedor"}))

    n = len(POSTS)
    out = await server.duplicar(id="120000000000000011", novo_nome="aberto 18-65 teste", quantidade=2, cliente="primos")
    enviados = posts_desde(n)
    copias = [f for p, f, _ in enviados if p.endswith("/copies")]
    nomes = [f["name"] for p, f, _ in enviados if not p.endswith("/copies")]
    check("duplicar: cópias PAUSADAS e com anúncios (deep_copy)", len(copias) == 2 and all(c["status_option"] == "PAUSED" and c["deep_copy"] == "true" for c in copias))
    check("duplicar: renomeia as cópias", nomes == ["aberto 18-65 teste 1", "aberto 18-65 teste 2"], str(nomes))
    out = await server.duplicar(id="120000000000000021", cliente="primos")
    check("duplicar anúncio: sem deep_copy", "deep_copy" not in POSTS[-1][1] and POSTS[-1][1]["status_option"] == "PAUSED")

    out = await server.criar_campanha(nome="[VENDAS] nova", orcamento_diario=200, estrategia_lance="limite_custo", cliente="primos")
    p, f, _ = POSTS[-1]
    check("criar campanha CBO: PAUSADA, orçamento e estratégia", p == "act_111111111111111/campaigns" and f["status"] == "PAUSED"
          and f["daily_budget"] == "20000" and f["bid_strategy"] == "COST_CAP" and f["special_ad_categories"] == "[]", out.splitlines()[-1][:90])
    await server.criar_campanha(nome="[VENDAS] sem cbo", cliente="primos")
    f = POSTS[-1][1]
    check("criar campanha sem orçamento: avisa a Meta que o orçamento fica nos conjuntos", f.get("is_adset_budget_sharing_enabled") == "false" and "daily_budget" not in f)
    out = await server.criar_campanha(nome="cara", orcamento_diario=900, cliente="primos")
    check("criar campanha acima do teto: recusada", out.startswith("ERRO") and "teto" in out)

    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="FSA 25-55", cliente="primos")
    check("conjunto em campanha sem orçamento exige orçamento do conjunto", out.startswith("ERRO") and "orcamento_diario" in out)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="FSA 25-55", orcamento_diario=100, estrategia_lance="limite_custo",
                                      cidades=["260131"], raio_km=25, idade_min=25, idade_max=55, genero="homens", publico_advantage=False, cliente="primos")
    check("estratégia de limite de custo exige limite_cpa", out.startswith("ERRO") and "limite_cpa" in out)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="FSA 25-55", orcamento_diario=100, estrategia_lance="limite_custo",
                                      limite_cpa=6, cidades=["260131"], raio_km=25, idade_min=25, idade_max=55, genero="homens",
                                      publico_advantage=False, cliente="primos")
    p, f, _ = POSTS[-1]
    alvo = json.loads(f["targeting"])
    check("criar conjunto: PAUSADO, conversão no pixel único da conta", p == "act_111111111111111/adsets" and f["status"] == "PAUSED"
          and json.loads(f["promoted_object"]) == {"pixel_id": "777000000000001", "custom_event_type": "PURCHASE"}
          and f["optimization_goal"] == "OFFSITE_CONVERSIONS", out.splitlines()[-1][:90])
    check("criar conjunto: orçamento, estratégia e limite em centavos", f["daily_budget"] == "10000" and f["bid_strategy"] == "COST_CAP" and f["bid_amount"] == "600")
    check("criar conjunto: segmentação (cidade com raio, idade, gênero, Advantage desligado)",
          alvo["geo_locations"] == {"location_types": ["home", "recent"], "cities": [{"key": "260131", "radius": 25, "distance_unit": "kilometer"}]}
          and alvo["age_min"] == 25 and alvo["age_max"] == 55 and alvo["genders"] == [1] and alvo["targeting_automation"] == {"advantage_audience": 0}, json.dumps(alvo)[:140])
    out = await server.criar_conjunto(campanha_id="120000000000000001", nome="x", orcamento_diario=50, cliente="primos")
    check("conjunto em campanha CBO não aceita orçamento próprio", out.startswith("ERRO") and "CBO" in out)

    n = len(POSTS)
    out = await server.criar_anuncio(conjunto_id="120000000000000011", nome="carro 01", pagina_id="888000000000001", instagram_id="17841400000000001",
                                     link="https://sorteiodosprimos.com.br", texto="Concorra a um carro!", titulo="Sorteio dos Primos",
                                     imagem_url="https://imagens.exemplo/carro.png", cta="SHOP_NOW", parametros_url="utm_source=meta", cliente="primos")
    caminhos = [p for p, _, _ in posts_desde(n)]
    check("criar anúncio: sobe a imagem, cria o criativo e o anúncio", caminhos == ["act_111111111111111/adimages", "act_111111111111111/adcreatives", "act_111111111111111/ads"], str(caminhos))
    img, cri, ad = [f for _, f, _ in posts_desde(n)]
    spec = json.loads(cri["object_story_spec"])
    check("imagem enviada em base64", base64.b64decode(img["bytes"]) == b"\x89PNG-imagem-de-teste")
    check("criativo: página, Instagram, link, texto, botão e UTM", spec["page_id"] == "888000000000001" and spec["instagram_user_id"] == "17841400000000001"
          and spec["link_data"]["image_hash"] == "hashnovo999" and spec["link_data"]["call_to_action"]["type"] == "SHOP_NOW" and cri["url_tags"] == "utm_source=meta")
    check("anúncio PAUSADO usando o criativo novo", ad["status"] == "PAUSED" and json.loads(ad["creative"])["creative_id"].startswith("120000"))
    check("download da imagem não leva o token", AUTH_IMAGEM == [None], str(AUTH_IMAGEM))
    out = await server.criar_anuncio(conjunto_id="120000000000000011", nome="video", pagina_id="888000000000001", link="https://x.com",
                                     texto="t", video_id="555000000000001", cliente="primos")
    spec = json.loads(POSTS[-2][1]["object_story_spec"])
    check("anúncio de vídeo usa a miniatura do vídeo", spec["video_data"]["image_url"] == "https://scontent.exemplo/thumb.jpg")
    out = await server.criar_anuncio(conjunto_id="120000000000000011", nome="sem midia", pagina_id="1", link="https://x", texto="t", cliente="primos")
    check("anúncio sem mídia: explica o que falta", out.startswith("ERRO") and "exatamente uma mídia" in out)

    # Segurança das repetições
    ESTADO["limite"] = 1; n = len(POSTS)
    await server.renomear(id="120000000000000021", nome="r1", cliente="primos")
    check("limite de chamadas: a Meta recusou antes de aplicar, então repete", [s for _, _, s in posts_desde(n)] == ["limite", "ok"])
    ESTADO["falha_500"] = True; n = len(POSTS)
    out = await server.criar_campanha(nome="dupla?", cliente="primos")
    check("falha 500 na criação: não repete e avisa que pode ter sido aplicada", [s for _, _, s in posts_desde(n)] == ["500"] and "PODE ter sido aplicada" in out)
    ESTADO["timeout"] = True; n = len(POSTS)
    out = await server.criar_campanha(nome="dupla?", cliente="primos")
    check("conexão caiu no envio: não repete e avisa", [s for _, _, s in posts_desde(n)] == ["timeout"] and "PODE ter sido aplicada" in out)

    # Isolamento entre clientes e leituras de apoio
    cliente("primos", "")
    out = await server.criar_campanha(nome="x", cliente="primos")
    check("permissão desligada de novo: recusa na hora, sem reiniciar nada", out.startswith("ERRO") and "não permite criar" in out)
    out = await server.alterar_status(ids=["120000000000000001"], acao="pausar", cliente="dona")
    check("permissão de um cliente não vale para a conta de outro", "não da conta padrão" in out)
    for nome, coro, esperado in (("pixels", server.listar_pixels(cliente="primos"), "Pixel Primos"),
                                 ("páginas", server.listar_paginas(cliente="primos"), "sorteiodosprimos"),
                                 ("mídias", server.listar_midias(cliente="primos"), "abc123hash"),
                                 ("localização", server.buscar_localizacao(termo="Feira de Santana", cliente="primos"), "260131")):
        r = await coro
        check(f"leitura de apoio: {nome}", esperado in r and not r.startswith("ERRO"), r.splitlines()[0][:80])
    out = await server.listar_clientes()
    check("listar_clientes mostra as permissões", "dona,dona,222222222222222,auto,ler+status+orcamento+editar+criar" in out, out.splitlines()[2][:100])

    # A Meta real pode não mandar metadata.type (ou mandar nomes antigos): o tipo tem que ser descoberto mesmo assim
    cliente("primos", "status,orcamento,editar,criar", teto="5000")
    for modo in ("sem", "legado"):
        ESTADO["metadata"] = modo
        n = len(POSTS)
        out = await server.alterar_orcamento(id="120000000000000001", orcamento_diario=500, cliente="primos")
        check(f"metadata {modo}: orçamento de campanha", out.startswith("Cliente") and posts_desde(n)[-1][:2] == ("120000000000000001", {"daily_budget": "50000"}), out[:120])
        n = len(POSTS)
        out = await server.alterar_orcamento(id="120000000000000011", orcamento_diario=130, cliente="primos")
        check(f"metadata {modo}: orçamento de conjunto", "OK conjunto" in out and posts_desde(n)[-1][0] == "120000000000000011", out[:120])
        out = await server.alterar_orcamento(id="120000000000000021", orcamento_diario=50, cliente="primos")
        check(f"metadata {modo}: anúncio continua recusado no orçamento", out.startswith("ERRO") and "anúncio" in out, out[:120])
        n = len(POSTS)
        out = await server.duplicar(id="120000000000000001", cliente="primos")
        check(f"metadata {modo}: duplica campanha com deep_copy e PAUSADA", posts_desde(n) and posts_desde(n)[0][0] == "120000000000000001/copies"
              and posts_desde(n)[0][1].get("deep_copy") == "true" and posts_desde(n)[0][1]["status_option"] == "PAUSED", out[:120])
        n = len(POSTS)
        out = await server.duplicar(id="120000000000000021", cliente="primos")
        check(f"metadata {modo}: duplica anúncio sem deep_copy", posts_desde(n) and "deep_copy" not in posts_desde(n)[0][1], out[:120])
    ESTADO["metadata"] = "normal"

    # Chave "Exigir período"
    cliente("primos", "status,orcamento,editar,criar", teto="1000")
    with open(tmp / "clientes/primos.env", "a") as f:
        f.write("EXIGIR_PERIODO=1\n")
    antes = sum("/insights" in u for u in URLS)
    out = await server.consultar_insights(cliente="primos")
    check("exigir período: consulta sem datas recusada e nada consultado", out.startswith("ERRO") and "exige período" in out
          and "pergunte" in out and sum("/insights" in u for u in URLS) == antes, out[:120])
    out = await server.consultar_insights(cliente="primos", desde="2026-10-13")
    check("exigir período: só a data inicial não basta", out.startswith("ERRO") and "exige período" in out)
    out = await server.exportar_relatorio(cliente="primos")
    check("exigir período: relatório sem datas recusado", out.startswith("ERRO") and "exige período" in out)
    out = await server.consultar_insights(cliente="primos", desde="2026-10-13", ate="2026-10-19")
    check("exigir período: com datas, consulta normal", not out.startswith("ERRO") and "2026-10-13 a 2026-10-19" in out, out[:120])
    out = await server.listar_clientes()
    check("listar_clientes mostra se exige período", ",sim," in out.splitlines()[2], out.splitlines()[2][:120])
    out = await server.estrutura_conta(cliente="primos", nivel="campanhas")
    check("estrutura sem período continua liberada para achar IDs", not out.startswith("ERRO") and "Só itens ativos" in out)

    # Estrutura filtrada pelo que veiculou no período
    FILTROS.clear()
    out = await server.estrutura_conta(cliente="primos", nivel="campanhas", desde="2026-10-13", ate="2026-10-19")
    dados = json.loads(out.split("## campanhas: ")[1].split("\n", 1)[1].splitlines()[0])
    check("período: só o que veiculou, pausado incluído, ordenado por gasto",
          [(d["campanha_id"], d["gasto_periodo"], d["status"]) for d in dados] ==
          [("120000000000000001", 300.0, "ACTIVE"), ("120000000000000002", 120.0, "PAUSED")], str(dados)[:160])
    check("período: o que não veiculou fica de fora (cpa_teste_7)", "cpa_teste_7" not in out)
    check("período: avisa o que veiculou mas não existe mais", "1 que veicularam no período já não aparecem" in out)
    check("período: busca na Meta só os IDs que veicularam", FILTROS and FILTROS[0][0] == "campaigns"
          and sorted(FILTROS[0][1][0]["value"]) == ["120000000000000001", "120000000000000002", "120000000000000099"], str(FILTROS)[:120])
    out = await server.estrutura_conta(cliente="primos", nivel="tudo", desde="2026-10-13", ate="2026-10-19")
    check("período, nível tudo: campanha, conjunto e anúncio do que veiculou",
          "## campanhas: 1" in out and "## conjuntos: 1" in out and "## anuncios: 1" in out and "120000000000000021" in out
          and "[VENDAS] cpa_600" not in out, out.replace("\n", " | ")[:160])
    out = await server.estrutura_conta(cliente="primos", nivel="campanhas", somente_ativos=False)
    check("sem período: lista pelo status, como antes", "cpa_teste_7" in out and "gasto_periodo" not in out)

    # Duplicar com orçamento da cópia
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000003", cliente="primos")
    check("duplicar cpa_teste_7 (R$ 6.000) sem orçamento: teto recusa sem criar", out.startswith("ERRO") and "passa do teto" in out and posts_desde(n) == [], out[:100])
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000003", orcamento_diario=1500, cliente="primos")
    check("duplicar com orçamento acima do teto: recusa sem criar", out.startswith("ERRO") and "passa do teto" in out and posts_desde(n) == [])
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000003", novo_nome="cpa_teste_7_b", quantidade=2, orcamento_diario=300, cliente="primos")
    env = [(p.split("/")[-1], sorted(f)) for p, f, _ in posts_desde(n)]
    orcs = [f["daily_budget"] for p, f, _ in posts_desde(n) if "daily_budget" in f]
    check("duplicar com orçamento: cópia, nome e orçamento de cada uma",
          [e[0] == "copies" for e in env] == [True, False, False, True, False, False] and orcs == ["30000", "30000"]
          and all(f["status_option"] == "PAUSED" for p, f, _ in posts_desde(n) if p.endswith("/copies")), str(env)[:160])
    check("duplicar com orçamento: resposta mostra o orçamento novo", out.count("OK ") == 2 and "orçamento diário BRL 300.00" in out, out[:160])
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000002", orcamento_diario=100, cliente="primos")
    check("campanha com orçamento nos conjuntos: orcamento_diario recusado sem criar", out.startswith("ERRO") and "orçamentos ficam nos conjuntos" in out and posts_desde(n) == [])
    out = await server.duplicar(id="120000000000000021", orcamento_diario=50, cliente="primos")
    check("anúncio com orcamento_diario: recusado", out.startswith("ERRO") and "Anúncio não tem orçamento" in out)
    cliente("primos", "status,orcamento,editar,criar", teto="150")
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000002", cliente="primos")
    check("teto vale para os conjuntos de campanha sem orçamento de campanha", out.startswith("ERRO") and "200.00" in out and posts_desde(n) == [], out[:120])
    cliente("primos", "status,criar", teto="1000")
    out = await server.duplicar(id="120000000000000003", orcamento_diario=300, cliente="primos")
    check("orçamento na cópia exige a permissão de orçamentos", out.startswith("ERRO") and "orçamentos e lances" in out)
    cliente("primos", "status,orcamento,editar,criar", teto="1000")
    ESTADO["falha_orcamento"] = True
    n = len(POSTS)
    out = await server.duplicar(id="120000000000000003", orcamento_diario=300, cliente="primos")
    ESTADO["falha_orcamento"] = False
    check("orçamento recusado depois da cópia: avisa, não repete e manda ajustar antes de ativar",
          "PARCIAL" in out and "NÃO foi trocado" in out and "6,000.00" in out and "Antes de ativar" in out
          and [s for _, _, s in posts_desde(n)] == ["ok", "recusado"], out[:200])

    tudo = "\n".join((tmp / "historico" / a).read_text() for a in os.listdir(tmp / "historico"))
    check("histórico sem token e sem bytes de imagem", not any(t in tudo for t in TOK.values()) and "bytes" not in tudo)
    check("histórico registra as falhas também", any(not h["ok"] for h in hist("primos")))
    check("token nunca em URL", not any(t in u for u in URLS for t in TOK.values()))

asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram"); sys.exit(0 if all(ok) else 1)
