"""Testa a 6.5.0 (teste A/B, catálogo, criativos avançados, pixel, comentários, BM, benchmark interno) contra uma Graph API simulada.
Uso: python testes/test_65.py <raiz do repositório>"""
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
TOKEN, PTOKEN = "TOKEN_USUARIO_hh88", "TOKEN_PAGINA_ii99"
ACC, BIZ, PG = "111111111111111", "555000000000001", "888000000000001"
def cli(nome, perms):
    (tmp / f"clientes/{nome}.env").write_text(f"NOME={nome}\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nMETA_BUSINESS_ID={BIZ}\nPERMISSOES={perms}\n")
cli("tudo", "criar,editar,status,bm"); cli("criar", "criar"); cli("leitor", "")
POSTS, URLS = [], []
OBJ = {"120000000000000001": "campaign", "120000000000000002": "campaign", "120000000000000011": "adset", "120000000000000021": "ad"}
CATS = [{"id": "4001", "name": "Loja"}]


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    auth = req.headers.get("authorization", "")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    if req.method == "POST":
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        POSTS.append((p, form, auth))
        if p.endswith("/items_batch"):
            return httpx.Response(200, json={"handles": ["h1"], "validation_status": []})
        return httpx.Response(200, json={"id": "7001", "success": True})
    if p == f"act_{ACC}":
        return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL"})
    if p in OBJ:
        o = {"id": p, "account_id": ACC, "name": "x", "metadata": {"type": OBJ[p]}}
        if OBJ[p] == "ad":
            o["creative"] = {"effective_object_story_id": f"{PG}_999000000001"}
        return httpx.Response(200, json=o)
    if p.endswith("/promote_pages"):
        return httpx.Response(200, json={"data": [{"id": PG, "name": "Pagina"}]})
    if p == PG:
        return httpx.Response(200, json={"id": PG, "name": "Pagina", "access_token": PTOKEN})
    if p.endswith("/product_catalogs"):
        return httpx.Response(200, json={"data": CATS})
    if p == "4001/product_feeds":
        return httpx.Response(200, json={"data": [{"id": "5001", "name": "Feed velho"}]})
    if p == "4001/product_sets":
        return httpx.Response(200, json={"data": [{"id": "6001", "name": "Todos"}]})
    if p.endswith("/adspixels"):
        return httpx.Response(200, json={"data": [{"id": "777", "name": "Pixel A"}]})
    if p == f"{PG}_999000000001/comments":
        assert auth == f"Bearer {PTOKEN}"
        return httpx.Response(200, json={"data": [{"id": f"{PG}_999000000001_77001", "message": "Golpe!", "from": {"name": "Fulano"}, "created_time": "2026-10-20T10:00:00+0000", "is_hidden": False, "comment_count": 0}]})
    if p.endswith("_77001"):
        return httpx.Response(200, json={"id": p, "post": {"id": f"{PG}_999000000001"}})
    if p.endswith("_88001"):
        return httpx.Response(200, json={"id": p, "post": {"id": "999999999999999_1"}})
    if p == f"{BIZ}/business_users":
        return httpx.Response(200, json={"data": [{"id": "910100001", "name": "Ana", "email": "ana@agencia.com", "role": "EMPLOYEE"}, {"id": "910200", "name": "Bia", "email": "bia@x.com", "role": "ADMIN"}]})
    if p == f"{BIZ}/system_users":
        return httpx.Response(200, json={"data": [{"id": "9301", "name": "sys", "role": "EMPLOYEE"}]})
    if p.startswith(f"{BIZ}/owned_"):
        return httpx.Response(200, json={"data": [{"id": "1", "name": p.split("/")[1]}]})
    if p.endswith("/insights"):
        L = lambda cid, nm, g, imp, cl, comp, val: {"account_id": ACC, "campaign_id": cid, "campaign_name": nm, "adset_id": cid, "adset_name": nm, "ad_id": cid, "ad_name": nm,
            "objective": "OUTCOME_SALES", "spend": str(g), "impressions": str(imp), "reach": "1", "frequency": "1", "inline_link_clicks": str(cl),
            "actions": [{"action_type": "purchase", "value": str(comp)}], "action_values": [{"action_type": "purchase", "value": str(val)}]}
        return httpx.Response(200, json={"data": [L("1001", "bom", 200, 20000, 400, 20, 2000), L("1002", "medio", 200, 20000, 200, 10, 1000),
                                                  L("1003", "ruim", 200, 40000, 100, 2, 200), L("1004", "pequeno", 5, 1000, 5, 0, 0)]})
    return err(400, 100, f"Unsupported get request {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []
def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))
def ultimo(): return POSTS[-1]


async def main():
    amanha = (date.today() + timedelta(days=1)).isoformat()
    cel = [{"nome": "A", "id": "120000000000000001"}, {"nome": "B", "id": "120000000000000002"}]
    n = len(POSTS)
    out = await server.criar_teste_ab(celulas=cel, cliente="leitor")
    check("A/B sem permissão: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.criar_teste_ab(celulas=cel, inicio=date.today().isoformat(), cliente="criar")
    check("A/B começando hoje: recusa", out.startswith("ERRO") and "amanhã" in out and len(POSTS) == n)
    out = await server.criar_teste_ab(celulas=[cel[0], {"nome": "B", "id": "120000000000000011"}], cliente="criar")
    check("A/B com tipos misturados: recusa sem criar", out.startswith("ERRO") and "mesmo tipo" in out and len(POSTS) == n, out[:100])
    out = await server.criar_teste_ab(celulas=[cel[0], {"nome": "a", "id": "120000000000000002"}], cliente="criar")
    check("A/B com nomes repetidos: recusa", out.startswith("ERRO") and "repetir" in out and len(POSTS) == n)
    out = await server.criar_teste_ab(celulas=cel, nome="Criativos", inicio=amanha, cliente="criar")
    p, f, _ = ultimo(); cs = json.loads(f["cells"])
    check("A/B criado: células de campanha com 50% cada", p == f"act_{ACC}/ad_studies" and f["type"] == "SPLIT_TEST" and [c["treatment_percentage"] for c in cs] == [50.0, 50.0]
          and cs[0]["campaigns"] == ["120000000000000001"] and f["start_time"].startswith(amanha), str(f)[:200])

    n = len(POSTS)
    out = await server.criar_catalogo(nome="Loja", cliente="criar")
    check("catálogo com nome repetido: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.criar_catalogo(nome="Outra", cliente="criar")
    check("segundo catálogo sem confirmar: recusa", out.startswith("ERRO") and "confirmar_novo" in out and len(POSTS) == n)
    out = await server.criar_catalogo(nome="Outra", confirmar_novo=True, cliente="criar")
    check("catálogo criado na BM", ultimo()[0] == f"{BIZ}/owned_product_catalogs" and ultimo()[1]["vertical"] == "commerce" and "7001" in out)
    n = len(POSTS)
    out = await server.criar_feed_produtos(catalogo_id="4001", nome="Feed novo", url="http://x.com/f.csv", cliente="criar")
    check("feed por http: recusa", out.startswith("ERRO") and "https" in out and len(POSTS) == n)
    out = await server.criar_feed_produtos(catalogo_id="4999", nome="Feed novo", url="https://x.com/f.csv", cliente="criar")
    check("feed em catálogo de outra conta: recusa", out.startswith("ERRO") and "não está ligado" in out and len(POSTS) == n)
    out = await server.criar_feed_produtos(catalogo_id="4001", nome="Feed novo", url="https://x.com/f.csv", cliente="criar")
    f = ultimo()[1]
    check("feed criado com agenda diária", json.loads(f["schedule"]) == {"interval": "DAILY", "url": "https://x.com/f.csv", "hour": 6} and f["default_currency"] == "BRL")
    out = await server.criar_regra_feed(catalogo_id="4001", feed_id="5999", atributo="description", tipo="mapear_coluna", valor="desc", cliente="criar")
    check("regra em feed de outro catálogo: recusa", out.startswith("ERRO") and "não é do catálogo" in out)
    out = await server.criar_regra_feed(catalogo_id="4001", feed_id="5001", atributo="description", tipo="mapear_coluna", valor="desc", cliente="criar")
    p, f, _ = ultimo()
    check("regra de feed criada", p == "5001/rules" and json.loads(f["params"]) == {"map_from": "desc"} and f["rule_type"] == "mapping_rule")
    out = await server.criar_conjunto_produtos(catalogo_id="4001", nome="Todos", filtro='{"price":{"gt":1}}', cliente="criar")
    check("conjunto de produtos repetido: recusa", out.startswith("ERRO") and "Já existe" in out)
    out = await server.criar_conjunto_produtos(catalogo_id="4001", nome="Caros", filtro="não é json", cliente="criar")
    check("filtro inválido: recusa", out.startswith("ERRO") and "JSON" in out)
    out = await server.criar_conjunto_produtos(catalogo_id="4001", nome="Caros", filtro='{"price":{"gt":10000}}', cliente="criar")
    check("conjunto de produtos criado", ultimo()[0] == "4001/product_sets" and json.loads(ultimo()[1]["filter"]) == {"price": {"gt": 10000}})

    prod = {"sku": "S1", "nome": "Tênis", "descricao": "Bom", "preco": "199,90", "estoque": "in stock", "url": "https://l/t", "imagem": "https://l/t.jpg"}
    n = len(POSTS)
    for rot, pr, trecho in [("sem sku", {**prod, "sku": ""}, "sku"), ("preço ruim", {**prod, "preco": "grátis"}, "preço inválido"), ("estoque ruim", {**prod, "estoque": "tem"}, "estoque inválido"),
                            ("url http", {**prod, "url": "http://l/t"}, "https"), ("campo estranho", {**prod, "cor": "azul"}, "desconhecidos"),
                            ("faltando campos", {"sku": "S2", "nome": "x"}, "faltam")]:
        out = await server.criar_produtos(catalogo_id="4001", produtos=[pr], cliente="criar")
        check(f"produto {rot}: recusa antes de enviar", out.startswith("ERRO") and trecho in out and len(POSTS) == n, out[:90])
    out = await server.criar_produtos(catalogo_id="4001", produtos=[prod], cliente="criar")
    p, f, _ = ultimo(); rq = json.loads(f["requests"])
    check("produtos criados em lote com preço no formato da Meta", p == "4001/items_batch" and rq[0]["method"] == "CREATE" and rq[0]["data"]["price"] == "199.90 BRL"
          and rq[0]["data"]["id"] == "S1" and f["allow_upsert"] == "true" and f["item_type"] == "PRODUCT_ITEM", str(f)[:200])
    out = await server.atualizar_produtos(catalogo_id="4001", produtos=[{"sku": "S1", "preco": "150", "estoque": "out of stock"}], cliente="criar")
    check("atualizar produto exige 'editar'", out.startswith("ERRO") and "renomear" in out)
    out = await server.atualizar_produtos(catalogo_id="4001", produtos=[{"sku": "S1", "preco": "150", "estoque": "out of stock"}], cliente="tudo")
    p, f, _ = ultimo(); rq = json.loads(f["requests"])
    check("atualizar produto: UPDATE sem upsert (SKU novo nunca é criado)", rq[0]["method"] == "UPDATE" and f["allow_upsert"] == "false" and rq[0]["data"]["price"] == "150.00 BRL")
    out = await server.atualizar_produtos(catalogo_id="4001", produtos=[{"sku": "S1", "visibilidade": "hidden"}], cliente="tudo")
    check("esconder produto = visibilidade hidden (não apaga)", json.loads(ultimo()[1]["requests"])[0]["data"]["visibility"] == "hidden")
    out = await server.criar_produtos(catalogo_id="4001", produtos=[prod, prod], cliente="criar")
    check("SKU repetido na lista: recusa", out.startswith("ERRO") and "repetidos" in out)

    n = len(POSTS)
    base = dict(nome="PAC", pagina_id=PG, formato="pac", link="https://x.com", textos=["Concorra"], cliente="criar")
    out = await server.criar_criativo_avancado(**base, midias=[{"imagem_hash": "h1"}, {"imagem_hash": "h2"}])
    check("PAC sem mídia padrão: recusa", out.startswith("ERRO") and "UMA mídia" in out and len(POSTS) == n)
    out = await server.criar_criativo_avancado(**base, midias=[{"imagem_hash": "h1", "plataformas": ["tiktok"]}, {"imagem_hash": "h2"}])
    check("PAC com plataforma inválida: recusa", out.startswith("ERRO") and "plataformas aceitas" in out)
    out = await server.criar_criativo_avancado(**base, midias=[{"imagem_hash": "h1", "plataformas": ["instagram"], "posicoes": ["story", "reels"]}, {"video_id": "55501"}])
    p, f, _ = ultimo(); afs = json.loads(f["asset_feed_spec"])
    check("PAC: regra por posicionamento e mídia padrão", p == f"act_{ACC}/adcreatives" and afs["optimization_type"] == "PLACEMENT"
          and afs["asset_customization_rules"][0]["customization_spec"] == {"publisher_platforms": ["instagram"], "instagram_positions": ["story", "reels"]}
          and afs["asset_customization_rules"][1]["customization_spec"] == {} and afs["images"][0]["hash"] == "h1" and afs["videos"][0]["video_id"] == "55501"
          and afs["bodies"] == [{"text": "Concorra"}] and afs["link_urls"] == [{"website_url": "https://x.com"}] and "criativo_id=7001" in out, str(afs)[:250])
    out = await server.criar_criativo_avancado(nome="MMU", pagina_id=PG, formato="mmu", link="https://x.com", textos=["a", "b"], titulos=["t"], midias=[{"imagem_hash": "h1"}, {"imagem_hash": "h2"}], cliente="criar")
    afs = json.loads(ultimo()[1]["asset_feed_spec"])
    check("MMU: várias mídias e variantes de texto, formato automático", afs["ad_formats"] == ["AUTOMATIC_FORMAT"] and len(afs["bodies"]) == 2 and len(afs["images"]) == 2 and afs["titles"] == [{"text": "t"}])
    out = await server.criar_criativo_avancado(nome="MMU", pagina_id=PG, formato="mmu", link="https://x.com", textos=["a"], midias=[{"imagem_hash": "h1", "video_id": "5"}, {"imagem_hash": "h2"}], cliente="criar")
    check("mídia com imagem E vídeo: recusa", out.startswith("ERRO") and "exatamente um" in out)
    out = await server.criar_criativo_avancado(nome="Cat", pagina_id=PG, formato="catalogo", link="https://x.com", textos=["Veja"], conjunto_produtos_id="6999", cliente="criar")
    check("catálogo com conjunto de outra conta: recusa", out.startswith("ERRO") and "não é de um catálogo" in out)
    out = await server.criar_criativo_avancado(nome="Cat", pagina_id=PG, formato="catalogo", link="https://x.com", textos=["Veja"], conjunto_produtos_id="6001", cliente="criar")
    f = ultimo()[1]; td = json.loads(f["object_story_spec"])["template_data"]
    check("carrossel de catálogo dinâmico", f["product_set_id"] == "6001" and td["name"] == "{{product.name}}" and td["link"] == "https://x.com")
    out = await server.criar_criativo_avancado(nome="AP", pagina_id=PG, formato="advantage_plus", link="https://x.com", textos=["Oi"], midias=[{"imagem_hash": "h1"}], melhorias=["image_touchups"], cliente="criar")
    f = ultimo()[1]
    check("Advantage+ criativo: melhorias OPT_IN", json.loads(f["degrees_of_freedom_spec"]) == {"creative_features_spec": {"image_touchups": {"enroll_status": "OPT_IN"}}})
    out = await server.criar_criativo_avancado(nome="AP", pagina_id=PG, formato="advantage_plus", link="https://x.com", textos=["Oi"], midias=[{"imagem_hash": "h1"}], melhorias=["Image Touch;"], cliente="criar")
    check("melhoria com nome estranho: recusa", out.startswith("ERRO") and "inválido" in out)
    out = await server.criar_criativo_avancado(nome="x", pagina_id="999999999999", formato="advantage_plus", link="https://x.com", textos=["Oi"], midias=[{"imagem_hash": "h1"}], cliente="criar")
    check("página de fora: recusa", out.startswith("ERRO") and "não está liberada" in out)

    n = len(POSTS)
    out = await server.criar_pixel(nome="Pixel A", cliente="criar")
    check("pixel com nome repetido: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.criar_pixel(nome="Pixel Novo", cliente="criar")
    check("pixel criado", ultimo()[0] == f"act_{ACC}/adspixels" and ultimo()[1] == {"name": "Pixel Novo"})

    CID = f"{PG}_999000000001_77001"
    out = await server.listar_comentarios(pagina_id=PG, anuncio_id="120000000000000021", cliente="leitor")
    check("comentários do anúncio lidos com o token da página", "Golpe!" in out and CID in out and "não" in out)
    out = await server.listar_comentarios(pagina_id=PG, post_id="999999999999999_1", cliente="leitor")
    check("post de outra página: recusa", out.startswith("ERRO") and "não é da página" in out)
    out = await server.listar_comentarios(pagina_id=PG, cliente="leitor")
    check("sem anúncio nem post: pede um", out.startswith("ERRO") and "OU" in out)
    n = len(POSTS)
    out = await server.moderar_comentario(comentario_id=CID, pagina_id=PG, cliente="criar")
    check("ocultar sem 'editar': recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.moderar_comentario(comentario_id=f"{PG}_1_88001", pagina_id=PG, cliente="tudo")
    check("comentário de post de outra página: recusa sem alterar", out.startswith("ERRO") and "não é de um post" in out and len(POSTS) == n)
    out = await server.moderar_comentario(comentario_id=CID, pagina_id=PG, cliente="tudo")
    p, f, a = ultimo()
    check("comentário ocultado (nunca apagado) com token da página", p == CID and f == {"is_hidden": "true"} and a == f"Bearer {PTOKEN}")
    out = await server.moderar_comentario(comentario_id=CID, pagina_id=PG, ocultar=False, cliente="tudo")
    check("comentário volta a aparecer", ultimo()[1] == {"is_hidden": "false"})
    out = await server.responder_comentario(comentario_id=CID, pagina_id=PG, texto="Obrigado pelo contato!", cliente="criar")
    p, f, a = ultimo()
    check("resposta pública como a página", p == f"{CID}/comments" and f == {"message": "Obrigado pelo contato!"} and a == f"Bearer {PTOKEN}")
    check("nenhum DELETE foi feito e o token da página não foi à URL", not any(PTOKEN in u for u in URLS))

    out = await server.listar_usuarios_bm(cliente="leitor")
    check("usuários da BM com e-mail mascarado", "Ana" in out and "a***@agencia.com" in out and "ana@agencia.com" not in out and "sistema" in out)
    out = await server.listar_ativos_bm(cliente="leitor")
    check("ativos da BM", "conta de anúncios" in out and "página" in out and "catálogo" in out)
    n = len(POSTS)
    out = await server.dar_acesso_ativo(tipo_ativo="conta_anuncios", ativo_id=ACC, usuario_id="910100001", tarefas=["anunciar"], cliente="tudo" if False else "criar")
    check("sem a permissão 'bm': recusa", out.startswith("ERRO") and "BM" in out and len(POSTS) == n)
    out = await server.dar_acesso_ativo(tipo_ativo="conta_anuncios", ativo_id=ACC, usuario_id="910100001", tarefas=["admin"], cliente="tudo")
    check("tarefa de administrador não existe", out.startswith("ERRO") and "não permitida" in out and len(POSTS) == n)
    out = await server.dar_acesso_ativo(tipo_ativo="conta_anuncios", ativo_id=ACC, usuario_id="9999999", tarefas=["anunciar"], cliente="tudo")
    check("usuário de fora da BM: recusa", out.startswith("ERRO") and "não é da BM" in out and len(POSTS) == n)
    out = await server.dar_acesso_ativo(tipo_ativo="conta_anuncios", ativo_id="999888777666555", usuario_id="910100001", tarefas=["anunciar"], cliente="tudo")
    check("conta não liberada ao cliente: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.dar_acesso_ativo(tipo_ativo="conta_anuncios", ativo_id=ACC, usuario_id="910100001", tarefas=["anunciar", "analisar"], cliente="tudo")
    p, f, _ = ultimo()
    check("acesso limitado dado à conta", p == f"act_{ACC}/assigned_users" and f["user"] == "910100001" and json.loads(f["tasks"]) == ["ADVERTISE", "ANALYZE"])
    out = await server.dar_acesso_ativo(tipo_ativo="pagina", ativo_id=PG, usuario_id="910100001", tarefas=["moderar"], cliente="tudo")
    check("acesso limitado dado à página", ultimo()[0] == f"{PG}/assigned_users" and json.loads(ultimo()[1]["tasks"]) == ["MODERATE"])

    d1, d2 = (date.today() - timedelta(days=7)).isoformat(), (date.today() - timedelta(days=1)).isoformat()
    out = await server.benchmark_interno(desde=d1, ate=d2, cliente="leitor")
    check("benchmark interno: ignora gasto pequeno e se declara comparativo INTERNO", "pequeno" not in out and "INTERNO" in out, out[:300])
    linhas = {l.split(",")[0]: l for l in out.splitlines() if l.startswith(("bom", "medio", "ruim"))}
    check("benchmark: 'bom' acima e 'ruim' abaixo do normal da conta", "acima do normal" in linhas["bom"] and "abaixo do normal" in linhas["ruim"] and "na média" in linhas["medio"], str(linhas)[:300])
    out = await server.benchmark_interno(desde=d1, ate=d2, gasto_minimo=1000, cliente="leitor")
    check("benchmark com pouco dado: avisa em vez de inventar", "pouco para comparar" in out)

    h = (tmp / "historico/tudo.jsonl").read_text()
    check("histórico: sem tokens e com as ferramentas novas", TOKEN not in h and PTOKEN not in h and "moderar_comentario" in h and "dar_acesso_ativo" in h and "atualizar_produtos" in h)


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
