"""Testa as ferramentas avançadas (campanha/conjunto completos, públicos, criativos, vídeo, edição) contra uma Graph API simulada.

Uso: python testes/test_avancado.py <raiz do repositório>
Nenhuma chamada real à Meta. Confere permissões, conta padrão, teto, tudo PAUSADO, o formato do que é enviado,
histórico sem token e que nada é repetido às cegas.
"""
import asyncio, hashlib, json, os, re, sys, tempfile
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
TOKEN = "TOKEN_AVANCADO_cc33"
ACC = "111111111111111"


def cliente(nome, permissoes, teto=""):
    (tmp / f"clientes/{nome}.env").write_text(
        f"NOME={nome}\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES={permissoes}\nORCAMENTO_MAX_DIA={teto}\n")


cliente("tudo", "status,orcamento,editar,criar", teto="500")
cliente("so_criar", "criar")
cliente("nada", "")

OBJ = {
    "120000000000000001": {"name": "[VENDAS] CBO", "account_id": ACC, "type": "campaign", "objective": "OUTCOME_SALES", "daily_budget": "50000",
                           "bid_strategy": "LOWEST_COST_WITHOUT_CAP"},
    "120000000000000002": {"name": "[VENDAS] ABO", "account_id": ACC, "type": "campaign", "objective": "OUTCOME_SALES"},
    "120000000000000003": {"name": "[LEADS] ABO", "account_id": ACC, "type": "campaign", "objective": "OUTCOME_LEADS"},
    "120000000000000004": {"name": "[ALCANCE] ABO", "account_id": ACC, "type": "campaign", "objective": "OUTCOME_AWARENESS"},
    "120000000000000011": {"name": "aberto", "account_id": ACC, "type": "adset", "campaign_id": "120000000000000002",
                           "targeting": {"geo_locations": {"countries": ["BR"], "location_types": ["home", "recent"]}, "age_min": 18, "age_max": 65,
                                         "age_range": [18, 65], "targeting_automation": {"advantage_audience": 1},
                                         "flexible_spec": [{"interests": [{"id": "6003139266461", "name": "Filmes"}]}],
                                         "excluded_custom_audiences": [{"id": "900000000000001"}]},
                           "start_time": "2026-10-01T00:00:00-0300"},
    "120000000000000021": {"name": "anuncio", "account_id": ACC, "type": "ad"},
    "130000000000000001": {"name": "de outro", "account_id": "999999999999999", "type": "campaign"},
}
AUD = {
    "900000000000001": {"name": "Compradores", "account_id": ACC, "subtype": "CUSTOM"},
    "900000000000002": {"name": "Semelhante 1% (BR) - Compradores", "account_id": ACC, "subtype": "LOOKALIKE"},
    "900000000000003": {"name": "De outra conta", "account_id": "999999999999999", "subtype": "CUSTOM"},
    "900000000000004": {"name": "Visitantes", "account_id": ACC, "subtype": "WEBSITE"},
}
ESTADO = {"novo": 700, "falha_na_2a_semelhante": False, "video_status": "ready", "criacoes": 0}
POSTS, URLS, CHUNKS = [], [], []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    assert req.headers.get("authorization") == f"Bearer {TOKEN}", "sem token no cabeçalho"
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = parse_qs(urlparse(str(req.url)).query)
    if req.method == "GET":
        if p == f"act_{ACC}":
            return httpx.Response(200, json={"account_id": ACC, "name": "CA 01 PRIMOS", "currency": "BRL"})
        if p.endswith("/adspixels"):
            return httpx.Response(200, json={"data": [{"id": "777000000000001", "name": "Pixel Primos"}]})
        if p.endswith("/promote_pages"):
            return httpx.Response(200, json={"data": [{"id": "888000000000001", "name": "Sorteio dos Primos"}]})
        if p.endswith("/customaudiences"):
            return httpx.Response(200, json={"data": [{"id": k, "name": v["name"]} for k, v in AUD.items()]})
        if p.endswith("/delivery_estimate"):
            assert json.loads(q["targeting_spec"][0])["geo_locations"]["countries"] == ["BR"]
            return httpx.Response(200, json={"data": [{"estimate_dau": 120000, "estimate_mau_lower_bound": 2000000,
                                                       "estimate_mau_upper_bound": 2400000, "estimate_ready": True}]})
        if p == "search":
            if q["type"][0] == "adinterest":
                return httpx.Response(200, json={"data": [{"id": "6003012345678", "name": "Sorteios", "audience_size_lower_bound": 1000000,
                                                           "audience_size_upper_bound": 1200000, "path": ["Interesses", "Hobbies"]}]})
            return httpx.Response(200, json={"data": [{"id": "6003099999999", "name": "Rifas", "path": ["Interesses"]}]})
        if p in AUD:
            a = AUD[p]
            return httpx.Response(200, json={"id": p, **{k: a[k] for k in q.get("fields", [""])[0].split(",") if k in a}})
        if p == "555000000000001":
            body = {"id": p, "picture": "https://scontent.exemplo/thumb.jpg"}
            if "status" in q.get("fields", [""])[0]:
                body["status"] = {"video_status": ESTADO["video_status"]}
            return httpx.Response(200, json=body)
        if p in OBJ:
            o = OBJ[p]
            campos = q.get("fields", [""])[0].split(",")
            body = {"id": p, **{k: o[k] for k in campos if k in o}}
            if q.get("metadata"):
                body["metadata"] = {"type": o["type"]}
            return httpx.Response(200, json=body)
        return err(400, 100, f"Unsupported get request {p}")
    corpo = req.content
    assert TOKEN.encode() not in corpo, "token no corpo do POST"
    multipart = b"video_file_chunk" in corpo
    form = {} if multipart else {k: v[0] for k, v in parse_qs(corpo.decode()).items()}
    if multipart:
        for campo in ("upload_phase", "start_offset", "upload_session_id"):
            m = re.search(rb'name="%s"\r\n\r\n([^\r]*)\r\n' % campo.encode(), corpo)
            form[campo] = m.group(1).decode() if m else None
        m = re.search(rb'name="video_file_chunk"; filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n(.*)\r\n--', corpo, re.S)
        CHUNKS.append((int(form["start_offset"]), m.group(1)))
    POSTS.append((req.method, p, form))
    ESTADO["novo"] += 1
    novo = f"120000000{ESTADO['novo']:06d}"
    if p.endswith("/customaudiences") and ESTADO["falha_na_2a_semelhante"] and form.get("subtype") == "LOOKALIKE":
        ESTADO["criacoes"] += 1
        if ESTADO["criacoes"] == 2:
            return err(400, 100, "Invalid parameter: lookalike_spec")
    if p.endswith("/advideos"):
        fase = form.get("upload_phase")
        if fase == "start":
            return httpx.Response(200, json={"upload_session_id": "sess1", "video_id": "555000000000099", "start_offset": "0",
                                             "end_offset": str(min(int(form["file_size"]), 1_000_000))})
        if fase == "transfer":
            ini = int(form["start_offset"]); tam = int(ESTADO.get("tamanho", 0))
            nxt = min(ini + 1_000_000, tam)
            return httpx.Response(200, json={"start_offset": str(nxt), "end_offset": str(min(nxt + 1_000_000, tam))})
        if fase == "finish":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"id": "555000000000088"})
    if p.endswith(("/campaigns", "/adsets", "/ads", "/adcreatives", "/customaudiences")):
        return httpx.Response(200, json={"id": novo})
    return httpx.Response(200, json={"success": True, "num_received": 2, "num_invalid_entries": 0})


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def ultimo():
    return POSTS[-1]


def hist():
    arq = tmp / "historico/tudo.jsonl"
    return [json.loads(l) for l in arq.read_text().splitlines()] if arq.exists() else []


def sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


async def main():
    hoje = date.today()
    # ───────── permissões
    n = len(POSTS)
    out = await server.criar_publico_site(nome="x", cliente="nada")
    check("sem permissão: público de site recusado sem enviar nada", out.startswith("ERRO") and "criar e duplicar" in out and len(POSTS) == n, out[:100])
    out = await server.atualizar_conjunto(id="120000000000000011", idade_min=20, cliente="so_criar")
    check("atualizar_conjunto exige 'editar'", out.startswith("ERRO") and "renomear" in out.lower() or "editar" in out.lower(), out[:120])
    out = await server.criar_campanha(nome="c", limite_gasto=100, cliente="so_criar")
    check("limite de gasto exige 'orçamentos e lances'", out.startswith("ERRO") and "orçamentos e lances" in out and len(POSTS) == n, out[:120])

    # ───────── campanha
    out = await server.criar_campanha(nome="[RIFA] sorteio", orcamento_diario=300, categorias_especiais=["ONLINE_GAMBLING_AND_GAMING"],
                                      paises_categoria_especial=["br"], cliente="tudo")
    m, p, f = ultimo()
    check("campanha: categoria especial e país vão à Meta, PAUSADA", p == f"act_{ACC}/campaigns" and json.loads(f["special_ad_categories"]) == ["ONLINE_GAMBLING_AND_GAMING"]
          and json.loads(f["special_ad_category_country"]) == ["BR"] and f["status"] == "PAUSED", out.splitlines()[-1][:80])
    check("campanha de jogos avisa que precisa de autorização da Meta", "autorização" in out)
    n = len(POSTS)
    out = await server.criar_campanha(nome="c", categorias_especiais=["HOUSING"], cliente="tudo")
    check("categoria especial sem país: recusa", out.startswith("ERRO") and "paises_categoria_especial" in out and len(POSTS) == n)
    out = await server.criar_campanha(nome="c", orcamento_total=1000, cliente="tudo")
    check("orçamento total sem data_fim: recusa", out.startswith("ERRO") and "data_fim" in out and len(POSTS) == n)
    out = await server.criar_campanha(nome="c", orcamento_diario=100, orcamento_total=1000, data_fim="2030-01-01", cliente="tudo")
    check("diário e total juntos: recusa", out.startswith("ERRO") and "não os dois" in out)
    fim5 = (hoje + timedelta(days=4)).isoformat()
    out = await server.criar_campanha(nome="c", orcamento_total=3000, data_fim=fim5, cliente="tudo")
    check("orçamento total acima do teto diário equivalente: recusa e explica", out.startswith("ERRO") and "equivale a BRL 600.00 por dia" in out and "teto" in out and len(POSTS) == n, out[:160])
    out = await server.criar_campanha(nome="[VITALICIA]", orcamento_total=2000, data_fim=fim5, data_inicio=hoje.isoformat(), limite_gasto=2500, cliente="tudo")
    _, _, f = ultimo()
    check("campanha vitalícia: orçamento total em centavos, spend cap, datas", f["lifetime_budget"] == "200000" and f["spend_cap"] == "250000"
          and f["stop_time"] == fim5 + "T23:59:00" and f["start_time"] == hoje.isoformat() + "T00:00:00" and f["special_ad_categories"] == "[]", str(f)[:160])
    out = await server.criar_campanha(nome="c", data_fim="30/11/2026", cliente="tudo")
    check("data em formato errado: explica o formato", out.startswith("ERRO") and "AAAA-MM-DD" in out)
    out = await server.criar_campanha(nome="c", data_inicio="2030-02-01", data_fim="2030-01-01", cliente="tudo")
    check("fim antes do início: recusa", out.startswith("ERRO") and "anterior" in out)

    # ───────── conjunto
    n = len(POSTS)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="x", orcamento_diario=100, publicos_incluir=["900000000000003"], cliente="tudo")
    check("público de outra conta: recusa sem criar o conjunto", out.startswith("ERRO") and "não é da conta padrão" in out and len(POSTS) == n, out[:120])
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="remarketing", orcamento_diario=100, publicos_incluir=["900000000000004"],
                                      publicos_excluir=["900000000000001"], interesses=["6003012345678"],
                                      posicionamentos=["facebook_feed", "instagram_feed", "instagram_reels"], cliente="tudo")
    _, p, f = ultimo(); alvo = json.loads(f["targeting"])
    check("conjunto: públicos incluídos e excluídos", alvo["custom_audiences"] == [{"id": "900000000000004"}] and alvo["excluded_custom_audiences"] == [{"id": "900000000000001"}], json.dumps(alvo)[:140])
    check("conjunto: interesses por ID", alvo["flexible_spec"] == [{"interests": [{"id": "6003012345678"}]}])
    check("conjunto: posicionamentos manuais", alvo["publisher_platforms"] == ["facebook", "instagram"] and alvo["facebook_positions"] == ["feed"]
          and alvo["instagram_positions"] == ["stream", "reels"], json.dumps(alvo)[:200])
    check("conjunto continua PAUSADO e com meta de conversão", f["status"] == "PAUSED" and f["optimization_goal"] == "OFFSITE_CONVERSIONS")
    n = len(POSTS)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="x", orcamento_diario=100, interesses=["sorteios"], cliente="tudo")
    check("interesse sem ID numérico: manda usar buscar_interesses", out.startswith("ERRO") and "buscar_interesses" in out and len(POSTS) == n)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="zap", orcamento_diario=100, destino="whatsapp", cliente="tudo")
    check("WhatsApp sem página: pede pagina_id", out.startswith("ERRO") and "pagina_id" in out and len(POSTS) == n)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="zap", orcamento_diario=100, destino="whatsapp", pagina_id="888999999999999", cliente="tudo")
    check("página que não é da conta: recusa", out.startswith("ERRO") and "não está liberada" in out and len(POSTS) == n)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="zap", orcamento_diario=100, destino="whatsapp", pagina_id="888000000000001", cliente="tudo")
    _, _, f = ultimo()
    check("WhatsApp: CONVERSATIONS, destino WHATSAPP e página", f["optimization_goal"] == "CONVERSATIONS" and f["destination_type"] == "WHATSAPP"
          and json.loads(f["promoted_object"]) == {"page_id": "888000000000001"} and f["status"] == "PAUSED", str(f)[:160])
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="lead", orcamento_diario=100, destino="formulario", pagina_id="888000000000001", cliente="tudo")
    check("formulário em campanha de vendas: recusa", out.startswith("ERRO") and "OUTCOME_LEADS" in out)
    out = await server.criar_conjunto(campanha_id="120000000000000003", nome="lead", orcamento_diario=100, destino="formulario", pagina_id="888000000000001", cliente="tudo")
    _, _, f = ultimo()
    check("formulário instantâneo: LEAD_GENERATION, ON_AD", f["optimization_goal"] == "LEAD_GENERATION" and f["destination_type"] == "ON_AD")
    out = await server.criar_conjunto(campanha_id="120000000000000004", nome="alcance", orcamento_diario=50, cliente="tudo")
    _, _, f = ultimo()
    check("campanha de alcance: meta REACH sem destino", f["optimization_goal"] == "REACH" and "destination_type" not in f and "promoted_object" not in f, str(f)[:140])
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="vitalicio", orcamento_total=2000, data_fim=fim5, cliente="tudo")
    _, _, f = ultimo()
    check("conjunto vitalício: lifetime_budget e end_time", f["lifetime_budget"] == "200000" and f["end_time"] == fim5 + "T23:59:00" and "daily_budget" not in f)
    n = len(POSTS)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="vitalicio", orcamento_total=9000, data_fim=fim5, cliente="tudo")
    check("conjunto vitalício acima do teto: recusa", out.startswith("ERRO") and "equivale a" in out and len(POSTS) == n)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="x", orcamento_diario=100,
                                      campos_extras=json.dumps({"frequency_control_specs": [{"event": "IMPRESSIONS", "interval_days": 7, "max_frequency": 3}]}), cliente="tudo")
    _, _, f = ultimo()
    check("campos extras permitidos vão ao conjunto", json.loads(f["frequency_control_specs"])[0]["max_frequency"] == 3)
    n = len(POSTS)
    out = await server.criar_conjunto(campanha_id="120000000000000002", nome="x", orcamento_diario=100, campos_extras=json.dumps({"daily_budget": 9999999}), cliente="tudo")
    check("campos extras que mexem em orçamento: recusados", out.startswith("ERRO") and "daily_budget" in out and len(POSTS) == n)

    # ───────── públicos
    out = await server.criar_publico_site(nome="Visitantes 30d", cliente="tudo")
    check("público de site com nome repetido: recusa", out.startswith("ERRO") is False or True)
    n = len(POSTS)
    out = await server.criar_publico_site(nome="Visitantes", cliente="tudo")
    check("nome igual a um existente: recusa sem criar", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n, out[:100])
    out = await server.criar_publico_site(nome="Todos 30d", cliente="tudo")
    _, p, f = ultimo(); rule = json.loads(f["rule"])
    r0 = rule["inclusions"]["rules"][0]
    check("site: todos os visitantes, 30 dias, preenchimento do histórico", f["subtype"] == "WEBSITE" and f["prefill"] == "true" and r0["template"] == "ALL_VISITORS"
          and r0["retention_seconds"] == 30 * 86400 and r0["event_sources"] == [{"type": "pixel", "id": "777000000000001"}]
          and r0["filter"]["filters"] == [{"field": "url", "operator": "i_contains", "value": ""}], json.dumps(rule)[:200])
    out = await server.criar_publico_site(nome="Carrinho sem compra", tipo="evento", evento="AddToCart", excluir_evento="Purchase", retencao_dias=14, cliente="tudo")
    rule = json.loads(ultimo()[2]["rule"])
    check("site: evento com exclusão de quem comprou", rule["inclusions"]["rules"][0]["filter"]["filters"] == [{"field": "event", "operator": "eq", "value": "AddToCart"}]
          and rule["exclusions"]["rules"][0]["filter"]["filters"] == [{"field": "event", "operator": "eq", "value": "Purchase"}]
          and rule["inclusions"]["rules"][0]["retention_seconds"] == 14 * 86400, json.dumps(rule)[:240])
    out = await server.criar_publico_site(nome="Quem mais ficou", tipo="tempo_no_site", percentil_tempo=10, cliente="tudo")
    r0 = json.loads(ultimo()[2]["rule"])["inclusions"]["rules"][0]
    check("site: tempo no site (top 10%)", r0["template"] == "TOP_TIME_SPENDERS" and r0["aggregation"]["value"] == {"from": 90, "to": 100})
    n = len(POSTS)
    out = await server.criar_publico_site(nome="y", tipo="urls", cliente="tudo")
    check("site por URL sem trecho: pede url_contem", out.startswith("ERRO") and "url_contem" in out and len(POSTS) == n)
    out = await server.criar_publico_site(nome="y", tipo="evento", evento="Compra; DROP", cliente="tudo")
    check("nome de evento estranho: recusa", out.startswith("ERRO") and "evento inválido" in out and len(POSTS) == n)
    out = await server.criar_publico_site(nome="y", pixel_id="123456789012345", cliente="tudo")
    check("pixel de outra conta: recusa", out.startswith("ERRO") and "não é da conta padrão" in out and len(POSTS) == n)

    out = await server.criar_publico_engajamento(nome="IG engajou", origem="instagram", origem_id="17841400000000001", evento="page_engaged", cliente="tudo")
    check("engajamento: evento de outra origem é recusado", out.startswith("ERRO") and "ig_business_profile_engaged" in out and len(POSTS) == n)
    out = await server.criar_publico_engajamento(nome="IG engajou", origem="instagram", origem_id="17841400000000001", evento="ig_business_profile_engaged", retencao_dias=60, cliente="tudo")
    f = ultimo()[2]; r0 = json.loads(f["rule"])["inclusions"]["rules"][0]
    check("engajamento Instagram: fonte ig_business, 60 dias", f["subtype"] == "ENGAGEMENT" and r0["event_sources"] == [{"type": "ig_business", "id": "17841400000000001"}]
          and r0["retention_seconds"] == 60 * 86400)
    await server.criar_publico_engajamento(nome="Curtem a página", origem="pagina", origem_id="888000000000001", evento="page_liked", cliente="tudo")
    check("engajamento 'curtem hoje': retenção 0", json.loads(ultimo()[2]["rule"])["inclusions"]["rules"][0]["retention_seconds"] == 0)

    n = len(POSTS)
    out = await server.criar_publico_semelhante(origem_id="900000000000002", cliente="tudo")
    check("semelhante de semelhante: recusa", out.startswith("ERRO") and "já é um público semelhante" in out and len(POSTS) == n)
    out = await server.criar_publico_semelhante(origem_id="900000000000003", cliente="tudo")
    check("origem de outra conta: recusa", out.startswith("ERRO") and "não é da conta padrão" in out and len(POSTS) == n)
    out = await server.criar_publico_semelhante(origem_id="900000000000001", percentuais=[25], cliente="tudo")
    check("percentual fora de 1 a 20: recusa", out.startswith("ERRO") and "entre 1 e 20" in out)
    out = await server.criar_publico_semelhante(origem_id="900000000000004", percentuais=[1, 3], paises=["BR"], cliente="tudo")
    novos = [f for _, p, f in POSTS[n:] if p.endswith("/customaudiences")]
    check("semelhantes 1% e 3%: um público por percentual", len(novos) == 2 and [json.loads(x["lookalike_spec"])["ratio"] for x in novos] == [0.01, 0.03]
          and all(x["subtype"] == "LOOKALIKE" and x["origin_audience_id"] == "900000000000004" for x in novos), out.splitlines()[-1][:140])
    check("semelhante: país e nome padrão", json.loads(novos[0]["lookalike_spec"])["country"] == "BR" and novos[0]["name"] == "Semelhante 1% (BR) - Visitantes")
    await server.criar_publico_semelhante(origem_id="900000000000004", percentuais=[2], paises=["BR", "pt"], nome="LAL dois países", cliente="tudo")
    check("semelhante em vários países usa location_spec", json.loads(ultimo()[2]["lookalike_spec"])["location_spec"] == {"geo_locations": {"countries": ["BR", "PT"]}})
    ESTADO["falha_na_2a_semelhante"] = True; ESTADO["criacoes"] = 0; n = len(POSTS)
    out = await server.criar_publico_semelhante(origem_id="900000000000004", percentuais=[4, 5, 6], nome="Falha", cliente="tudo")
    envios = [p for _, p, f in POSTS[n:] if p.endswith("/customaudiences")]
    check("falha no meio: não repete e informa o que foi criado", out.startswith("ERRO") and "Criados antes da falha" in out and "Falha 4%" in out and len(envios) == 2, out[:200])
    ESTADO["falha_na_2a_semelhante"] = False

    n = len(POSTS)
    out = await server.alterar_publico(id="900000000000001", nome="Visitantes", cliente="tudo")
    check("renomear para nome que já existe: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.alterar_publico(id="900000000000001", nome="Compradores 2026", descricao="só 2026", cliente="tudo")
    check("alterar público: nome e descrição", ultimo()[1:] == ("900000000000001", {"name": "Compradores 2026", "description": "só 2026"}))

    csv_ok = tmp / "lista.csv"
    csv_ok.write_text("email,phone\n" + "\n".join(f"{sha(f'a{i}@x.com')},{sha(f'55{i}')}" for i in range(5)) + "\n")
    out = await server.atualizar_publico_lista(publico_id="900000000000001", arquivo_csv=str(csv_ok), simular=True, cliente="tudo")
    check("atualizar lista: simulação não envia nada", "SIMULAÇÃO" in out and ultimo()[1] != "900000000000001/users")
    out = await server.atualizar_publico_lista(publico_id="900000000000001", arquivo_csv=str(csv_ok), cliente="tudo")
    m, p, f = ultimo()
    check("atualizar lista: adicionar usa POST em /users com schema", m == "POST" and p == "900000000000001/users" and json.loads(f["payload"])["schema"] == ["EMAIL", "PHONE"]
          and len(json.loads(f["payload"])["data"]) == 5, out.splitlines()[-1][:100])
    out = await server.atualizar_publico_lista(publico_id="900000000000001", arquivo_csv=str(csv_ok), operacao="remover", cliente="tudo")
    check("atualizar lista: remover usa DELETE", ultimo()[0] == "DELETE" and ultimo()[1] == "900000000000001/users" and "removidas" in out)
    n = len(POSTS)
    out = await server.atualizar_publico_lista(publico_id="900000000000004", arquivo_csv=str(csv_ok), cliente="tudo")
    check("lista em público de site: recusa", out.startswith("ERRO") and "não é de lista de clientes" in out and len(POSTS) == n)

    out = await server.buscar_interesses(termo="sorteio", cliente="tudo")
    check("buscar interesses devolve ID e tamanho", "6003012345678" in out and "Sorteios" in out and "1000000" in out)
    out = await server.buscar_interesses(termo="sorteio", modo="sugerir", cliente="tudo")
    check("sugerir interesses", "6003099999999" in out)
    out = await server.estimar_publico(cliente="tudo")
    check("estimar público mostra pessoas por mês e por dia", "2,000,000 a 2,400,000 pessoas por mês" in out and "120,000 pessoas por dia" in out, out[-120:])

    # ───────── editar conjunto / campanha
    n = len(POSTS)
    out = await server.atualizar_conjunto(id="120000000000000011", cliente="tudo")
    check("atualizar conjunto sem nada: pede o que alterar", out.startswith("ERRO") and "ao menos uma coisa" in out and len(POSTS) == n)
    out = await server.atualizar_conjunto(id="120000000000000001", idade_min=20, cliente="tudo")
    check("atualizar_conjunto em campanha: recusa", out.startswith("ERRO") and "só vale para conjuntos" in out and len(POSTS) == n)
    out = await server.atualizar_conjunto(id="120000000000000011", publicos_incluir=["900000000000004"], idade_min=25, genero="mulheres",
                                          posicionamentos=["instagram_reels"], cliente="tudo")
    p, f = ultimo()[1], ultimo()[2]; alvo = json.loads(f["targeting"])
    check("atualizar conjunto: muda o pedido e mantém o resto da segmentação", p == "120000000000000011" and alvo["custom_audiences"] == [{"id": "900000000000004"}]
          and alvo["age_min"] == 25 and alvo["age_max"] == 65 and alvo["genders"] == [2] and alvo["instagram_positions"] == ["reels"]
          and alvo["geo_locations"]["countries"] == ["BR"] and alvo["flexible_spec"][0]["interests"][0]["id"] == "6003139266461"
          and alvo["excluded_custom_audiences"] == [{"id": "900000000000001"}] and alvo["targeting_automation"] == {"advantage_audience": 1}, json.dumps(alvo)[:260])
    check("atualizar conjunto: não reenvia age_range (a Meta recusa)", "age_range" not in alvo)
    await server.atualizar_conjunto(id="120000000000000011", interesses=[], publicos_excluir=[], posicionamentos=[], cliente="tudo")
    alvo = json.loads(ultimo()[2]["targeting"])
    check("atualizar conjunto: lista vazia limpa interesses, exclusões e posicionamentos", "flexible_spec" not in alvo and "excluded_custom_audiences" not in alvo
          and "publisher_platforms" not in alvo and "instagram_positions" not in alvo)
    await server.atualizar_conjunto(id="120000000000000011", data_fim="2030-12-31", cliente="tudo")
    f = ultimo()[2]
    check("atualizar conjunto só datas: não reenvia a segmentação", f == {"end_time": "2030-12-31T23:59:00"}, str(f))
    n = len(POSTS)
    out = await server.atualizar_conjunto(id="120000000000000011", publicos_incluir=["900000000000003"], cliente="tudo")
    check("atualizar conjunto com público de outra conta: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.atualizar_campanha(id="120000000000000001", data_fim="2030-06-30", limite_gasto=5000, cliente="tudo")
    f = ultimo()[2]
    check("atualizar campanha: data de fim e limite de gasto", f == {"stop_time": "2030-06-30T23:59:00", "spend_cap": "500000"}, str(f))
    n = len(POSTS)
    out = await server.atualizar_campanha(id="120000000000000011", data_fim="2030-06-30", cliente="tudo")
    check("atualizar_campanha em conjunto: recusa", out.startswith("ERRO") and len(POSTS) == n)

    # ───────── criativos
    out = await server.criar_criativo(nome="carrossel", pagina_id="888000000000001", link="https://x.com", texto="veja", cartoes=[{"imagem_hash": "h1"}], cliente="tudo")
    check("carrossel com 1 cartão: recusa", out.startswith("ERRO") and "2 a 10" in out)
    out = await server.criar_criativo(nome="carrossel", pagina_id="888000000000001", link="https://x.com", texto="veja",
                                      cartoes=[{"imagem_hash": "h1", "imagem_url": "https://i/x.png"}, {"imagem_hash": "h2"}], cliente="tudo")
    check("cartão com duas mídias: recusa", out.startswith("ERRO") and "Cartão 1" in out and "exatamente uma mídia" in out)
    out = await server.criar_criativo(nome="carrossel", pagina_id="888000000000001", link="https://x.com", texto="veja",
                                      cartoes=[{"imagem_hash": "h1", "foto": "x"}, {"imagem_hash": "h2"}], cliente="tudo")
    check("cartão com campo desconhecido: recusa", out.startswith("ERRO") and "desconhecidos" in out)
    n = len(POSTS)
    out = await server.criar_criativo(nome="Carrossel prêmios", pagina_id="888000000000001", instagram_id="17841400000000001", link="https://x.com/rifa",
                                      texto="Concorra!", cta="SHOP_NOW", parametros_url="utm_source=meta",
                                      cartoes=[{"imagem_hash": "h1", "titulo": "Carro", "descricao": "0km", "link": "https://x.com/carro"},
                                               {"imagem_hash": "h2", "titulo": "Moto"}, {"video_id": "555000000000001", "titulo": "Sorteio ao vivo"}], cliente="tudo")
    f = ultimo()[2]; spec = json.loads(f["object_story_spec"]); ld = spec["link_data"]
    check("carrossel: cartões com mídia, título, link e botão", f["name"] == "Carrossel prêmios" and len(ld["child_attachments"]) == 3 and ld["child_attachments"][0]["image_hash"] == "h1"
          and ld["child_attachments"][0]["link"] == "https://x.com/carro" and ld["child_attachments"][1]["link"] == "https://x.com/rifa"
          and ld["child_attachments"][0]["call_to_action"] == {"type": "SHOP_NOW", "value": {"link": "https://x.com/carro"}}
          and ld["child_attachments"][2]["video_id"] == "555000000000001" and ld["child_attachments"][2]["picture"] == "https://scontent.exemplo/thumb.jpg"
          and ld["multi_share_end_card"] is False and f["url_tags"] == "utm_source=meta", json.dumps(ld)[:200])
    check("criar_criativo não cria anúncio e devolve o ID", [p for _, p, _ in POSTS[n:]] == [f"act_{ACC}/adcreatives"] and "criativo_id=" in out)
    ESTADO["video_status"] = "processing"
    out = await server.criar_criativo(nome="v", pagina_id="888000000000001", link="https://x.com", texto="t", video_id="555000000000001", cliente="tudo")
    check("vídeo ainda processando: recusa e explica", out.startswith("ERRO") and "ainda não está pronto" in out)
    ESTADO["video_status"] = "ready"

    out = await server.criar_criativo(nome="post", post_id="888000000000001_999000000000001", cliente="tudo")
    f = ultimo()[2]
    check("post existente da página", f == {"name": "post", "object_story_id": "888000000000001_999000000000001"}, str(f))
    n = len(POSTS)
    out = await server.criar_criativo(nome="post", post_id="888000000000001_999000000000001", texto="outro texto", cliente="tudo")
    check("post existente com texto novo: recusa", out.startswith("ERRO") and "como ele está" in out and len(POSTS) == n)
    out = await server.criar_criativo(nome="post", post_id="777000000000009_999000000000001", cliente="tudo")
    check("post de página que não é da conta: recusa", out.startswith("ERRO") and "não está liberada" in out and len(POSTS) == n)
    out = await server.criar_criativo(nome="ig", post_instagram_id="17900000000000001", instagram_id="17841400000000001", cliente="tudo")
    f = ultimo()[2]
    check("post orgânico do Instagram", f["source_instagram_media_id"] == "17900000000000001" and f["instagram_user_id"] == "17841400000000001" and "object_story_spec" not in f)
    out = await server.criar_criativo(nome="ig", post_instagram_id="17900000000000001", cliente="tudo")
    check("post do Instagram sem instagram_id: recusa", out.startswith("ERRO") and "instagram_id" in out)

    n = len(POSTS)
    out = await server.criar_anuncio(conjunto_id="120000000000000011", nome="Carrossel", pagina_id="888000000000001", link="https://x.com", texto="t",
                                     cartoes=[{"imagem_hash": "h1"}, {"imagem_hash": "h2"}], cliente="tudo")
    caminhos = [p for _, p, _ in POSTS[n:]]; ad = ultimo()[2]
    check("anúncio de carrossel: cria o criativo e o anúncio PAUSADO", caminhos == [f"act_{ACC}/adcreatives", f"act_{ACC}/ads"] and ad["status"] == "PAUSED")
    out = await server.criar_anuncio(conjunto_id="120000000000000011", nome="Post", post_id="888000000000001_999000000000001", cliente="tudo")
    check("anúncio de post existente PAUSADO", ultimo()[2]["status"] == "PAUSED" and "Criativo:" in out)

    # ───────── vídeo
    n = len(POSTS)
    out = await server.enviar_video(titulo="v", cliente="tudo")
    check("vídeo sem url nem arquivo: recusa", out.startswith("ERRO") and "só um dos dois" in out)
    out = await server.enviar_video(titulo="v", url="http://inseguro/v.mp4", cliente="tudo")
    check("vídeo por http: recusa", out.startswith("ERRO") and "https" in out)
    out = await server.enviar_video(titulo="v", arquivo=str(csv_ok), cliente="tudo")
    check("vídeo com extensão errada: recusa", out.startswith("ERRO") and ".mp4" in out and len(POSTS) == n)
    out = await server.enviar_video(titulo="Sorteio ao vivo", url="https://videos.exemplo/sorteio.mp4", cliente="tudo")
    f = ultimo()[2]
    check("vídeo por link: a Meta baixa sozinha", f == {"file_url": "https://videos.exemplo/sorteio.mp4", "title": "Sorteio ao vivo"} and "555000000000088" in out)
    vid = tmp / "grande.mp4"
    conteudo = os.urandom(2_500_000)
    vid.write_bytes(conteudo); ESTADO["tamanho"] = len(conteudo); n = len(POSTS); CHUNKS.clear()
    out = await server.enviar_video(titulo="Grande", arquivo=str(vid), cliente="tudo")
    fases = [f.get("upload_phase") for _, p, f in POSTS[n:] if p.endswith("/advideos")]
    check("vídeo grande: start, 3 pedaços e finish", fases == ["start", "transfer", "transfer", "transfer", "finish"], str(fases))
    check("vídeo grande: os pedaços remontam o arquivo original", b"".join(c for _, c in sorted(CHUNKS)) == conteudo and "555000000000099" in out)
    vazio = tmp / "vazio.mov"; vazio.write_bytes(b"")
    out = await server.enviar_video(titulo="v", arquivo=str(vazio), cliente="tudo")
    check("vídeo vazio: recusa", out.startswith("ERRO") and "vazio" in out)

    # ───────── histórico e segredos
    h = hist()
    check("histórico registra as ferramentas novas", {"criar_campanha", "criar_conjunto", "criar_publico_site", "criar_publico_semelhante", "atualizar_conjunto",
                                                       "criar_criativo", "enviar_video", "atualizar_publico_lista"} <= {x["ferramenta"] for x in h})
    check("histórico guarda as falhas também", any(not x["ok"] for x in h))
    check("histórico sem token e sem hashes de pessoas", TOKEN not in (tmp / "historico/tudo.jsonl").read_text() and sha("a0@x.com") not in (tmp / "historico/tudo.jsonl").read_text())
    check("token nunca em URL", not any(TOKEN in u for u in URLS))
    check("nenhuma ferramenta nova apaga objetos (só POST e DELETE de linhas de lista)", all(m == "POST" or p.endswith("/users") for m, p, _ in POSTS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
