"""Testa a 6.4.0 (formulários e leads, regras automáticas, API de Conversões) contra uma Graph API simulada.
Uso: python testes/test_leads_regras_capi.py <raiz do repositório>"""
import asyncio, hashlib, json, os, sys, tempfile
from datetime import datetime, timedelta, timezone
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
TOKEN, PTOKEN = "TOKEN_USUARIO_ff66", "TOKEN_PAGINA_gg77"
ACC = "111111111111111"
(tmp / "clientes/c.env").write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=criar\n")
(tmp / "clientes/tudo.env").write_text(f"NOME=tudo\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=criar,status\n")
(tmp / "clientes/leitor.env").write_text(f"NOME=leitor\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={ACC}\nPERMISSOES=\n")
POSTS, URLS, AUTH = [], [], []
LEADS = [{"id": f"90{i}", "created_time": "2026-10-20T10:00:00+0000", "ad_name": "ad1", "adset_name": "cj", "campaign_name": "camp",
          "field_data": [{"name": "full_name", "values": [f"Pessoa {i}"]}, {"name": "email", "values": [f"pessoa{i}@exemplo.com"]},
                         {"name": "phone_number", "values": [f"+5511999990{i:03d}"]}]} for i in range(3)]


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    URLS.append(str(req.url))
    auth = req.headers.get("authorization", "")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    AUTH.append((p, auth))
    if req.method == "POST":
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        POSTS.append((p, form, auth))
        if p.endswith("/events"):
            return httpx.Response(200, json={"events_received": len(json.loads(form["data"])), "fbtrace_id": "x"})
        return httpx.Response(200, json={"id": "7001"})
    if p == f"act_{ACC}":
        return httpx.Response(200, json={"account_id": ACC, "name": "CA 01", "currency": "BRL"})
    if p.endswith("/promote_pages"):
        return httpx.Response(200, json={"data": [{"id": "888000000000001", "name": "Sorteio dos Primos"}]})
    if p == "888000000000001":
        return httpx.Response(200, json={"id": p, "name": "Sorteio dos Primos", "access_token": PTOKEN})
    if p == "888000000000001/leadgen_forms":
        assert auth == f"Bearer {PTOKEN}", "formulários exigem o token da página"
        return httpx.Response(200, json={"data": [{"id": "600100001", "name": "Existente", "status": "ACTIVE", "leads_count": 3, "created_time": "2026-10-01T00:00:00+0000",
                                                  "questions": [{"key": "email", "type": "EMAIL"}, {"key": "phone_number", "type": "PHONE"}]}]})
    if p == "600100001/leads":
        assert auth == f"Bearer {PTOKEN}"
        if "filtering" in q:
            f = json.loads(q["filtering"]); assert f[0]["field"] == "time_created"
        return httpx.Response(200, json={"data": LEADS})
    if p.endswith("/adrules_library"):
        return httpx.Response(200, json={"data": [{"id": "5001", "name": "Regra velha", "status": "ENABLED", "execution_spec": {"execution_type": "NOTIFICATION"},
                                                  "schedule_spec": {"schedule_type": "DAILY"}, "evaluation_spec": {"filters": [{"field": "spent", "value": 1000}]}}]})
    if p.endswith("/adspixels"):
        return httpx.Response(200, json={"data": [{"id": "777000000000001", "name": "Pixel A"}]})
    return err(400, 100, f"Unsupported get request {p}")


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def sha(x): return hashlib.sha256(x.encode()).hexdigest()


async def main():
    PG = "888000000000001"
    out = await server.listar_formularios(pagina_id=PG, cliente="leitor")
    check("formulários listados com o token da página", "Existente" in out and "email, phone_number" in out and any(a == f"Bearer {PTOKEN}" for pth, a in AUTH if pth.endswith("leadgen_forms")))
    out = await server.listar_formularios(pagina_id="999999999999", cliente="leitor")
    check("página fora da conta: recusa", out.startswith("ERRO") and "não está liberada" in out)
    check("token da página nunca foi à URL", not any(PTOKEN in u for u in URLS))

    out = await server.baixar_leads(formulario_id="600100001", pagina_id=PG, desde="2026-10-01", cliente="leitor")
    arq = Path(next(l for l in out.splitlines() if l.startswith("Arquivo:"))[len("Arquivo: "):])
    txt = arq.read_text(encoding="utf-8")
    check("leads baixados: CSV completo com colunas das perguntas", "pessoa1@exemplo.com" in txt and "full_name" in txt and txt.count("\n") == 4, txt[:120])
    check("arquivo com permissão restrita (600)", (arq.stat().st_mode & 0o777) == 0o600 and "relatorios/leitor" in str(arq))
    check("resposta só mostra amostra mascarada", "pessoa1@exemplo.com" not in out and "p***@exemplo.com" in out and "***0001" in out and "3 lead" in out, out[-300:])
    out = await server.baixar_leads(formulario_id="699900001", pagina_id=PG, cliente="leitor")
    check("formulário de outra página: recusa", out.startswith("ERRO") and "não é da página" in out)
    out = await server.baixar_leads(formulario_id="600100001", pagina_id=PG, desde="2026-10-10", ate="2026-10-01", cliente="leitor")
    check("período invertido: recusa", out.startswith("ERRO") and "depois" in out)

    n = len(POSTS)
    out = await server.criar_formulario_leads(pagina_id=PG, nome="Novo", politica_privacidade_url="https://x.com/p", cliente="leitor")
    check("criar formulário sem permissão: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.criar_formulario_leads(pagina_id=PG, nome="Existente", politica_privacidade_url="https://x.com/p", cliente="c")
    check("nome repetido: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.criar_formulario_leads(pagina_id=PG, nome="Novo", politica_privacidade_url="http://x.com/p", cliente="c")
    check("política sem https: recusa", out.startswith("ERRO") and "https" in out and len(POSTS) == n)
    out = await server.criar_formulario_leads(pagina_id=PG, nome="Novo", politica_privacidade_url="https://x.com/p", perguntas=["rg"], cliente="c")
    check("pergunta desconhecida: recusa", out.startswith("ERRO") and "desconhecida" in out and len(POSTS) == n)
    out = await server.criar_formulario_leads(pagina_id=PG, nome="Novo", politica_privacidade_url="https://x.com/p", url_agradecimento="https://chat.whatsapp.com/abc", cliente="c")
    pth, f, auth = POSTS[-1]
    check("formulário criado na página com o token da página", pth == f"{PG}/leadgen_forms" and auth == f"Bearer {PTOKEN}" and json.loads(f["questions"]) == [{"type": "FULL_NAME"}, {"type": "EMAIL"}, {"type": "PHONE"}]
          and json.loads(f["thank_you_page"])["website_url"] == "https://chat.whatsapp.com/abc" and "7001" in out, str(f)[:200])
    h = (tmp / "historico/c.jsonl").read_text()
    check("histórico sem nenhum dos tokens", TOKEN not in h and PTOKEN not in h and "criar_formulario_leads" in h)

    n = len(POSTS)
    base = dict(nome="CPA alto", entidade="conjunto", metrica="custo_por_resultado", operador=">", valor=40, janela="3_dias")
    out = await server.criar_regra_automatica(**base, cliente="leitor")
    check("regra sem permissão: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.criar_regra_automatica(**base, acao="pausar", cliente="c")
    check("regra que pausa exige também 'ativar e pausar'", out.startswith("ERRO") and "ativar e pausar" in out and len(POSTS) == n)
    out = await server.criar_regra_automatica(**dict(base, nome="Regra velha"), cliente="c")
    check("nome de regra repetido: recusa", out.startswith("ERRO") and "Já existe" in out and len(POSTS) == n)
    out = await server.criar_regra_automatica(**base, cliente="c")
    pth, f, _ = POSTS[-1]; fil = json.loads(f["evaluation_spec"])["filters"]
    check("regra nasce DESLIGADA e só avisa", pth == f"act_{ACC}/adrules_library" and f["status"] == "DISABLED" and json.loads(f["execution_spec"]) == {"execution_type": "NOTIFICATION"}
          and "DESLIGADA" in out, str(f)[:200])
    check("filtros: entidade, janela e valor em centavos", {"field": "entity_type", "value": "ADSET", "operator": "EQUAL"} in fil and {"field": "time_preset", "value": "LAST_3_DAYS", "operator": "EQUAL"} in fil
          and {"field": "cost_per_result", "value": 4000, "operator": "GREATER_THAN"} in fil, str(fil))
    out = await server.criar_regra_automatica(**dict(base, nome="Pausa", acao="pausar", metrica="frequencia", valor=3.5, gasto_minimo=50), cliente="tudo")
    pth, f, _ = POSTS[-1]; fil = json.loads(f["evaluation_spec"])["filters"]
    check("regra de pausar: PAUSE, frequência sem centavos, gasto mínimo", json.loads(f["execution_spec"])["execution_type"] == "PAUSE" and {"field": "frequency", "value": 3.5, "operator": "GREATER_THAN"} in fil
          and {"field": "spent", "value": 5000, "operator": "GREATER_THAN"} in fil and f["status"] == "DISABLED")
    out = await server.criar_regra_automatica(**dict(base, nome="Ligada"), ativar=True, cliente="c")
    check("ativar=True exige 'ativar e pausar'", out.startswith("ERRO") and "ativar e pausar" in out)
    out = await server.listar_regras(cliente="leitor")
    check("regras listadas", "Regra velha" in out and "NOTIFICATION" in out)

    n = len(POSTS)
    PX = "777000000000001"
    agora = datetime.now(timezone.utc)
    ev = {"evento": "Purchase", "email": " Fulano@Exemplo.com ", "telefone": "+55 (11) 99999-8888", "valor": 150, "fonte": "whatsapp", "id_evento": "v1"}
    out = await server.enviar_eventos_conversao(pixel_id=PX, eventos=[ev], cliente="leitor")
    check("eventos sem permissão: recusa", out.startswith("ERRO") and len(POSTS) == n)
    out = await server.enviar_eventos_conversao(pixel_id="999999999999", eventos=[ev], cliente="c")
    check("pixel de outra conta: recusa sem enviar", out.startswith("ERRO") and "não é da conta" in out and len(POSTS) == n)
    for rot, e, trecho in [("sem identificador", {"evento": "Lead", "fonte": "whatsapp"}, "email, telefone"),
                           ("evento inválido", {"evento": "Compra!", "email": "a@b.com", "fonte": "whatsapp"}, "inválido"),
                           ("telefone curto", {"evento": "Lead", "telefone": "123", "fonte": "whatsapp"}, "curto"),
                           ("site sem url", {"evento": "Lead", "email": "a@b.com"}, "url"),
                           ("data antiga", {"evento": "Lead", "email": "a@b.com", "fonte": "whatsapp", "quando": (agora - timedelta(days=9)).strftime("%Y-%m-%d %H:%M")}, "7 dias"),
                           ("fonte inválida", {"evento": "Lead", "email": "a@b.com", "fonte": "tv"}, "fonte inválida")]:
        out = await server.enviar_eventos_conversao(pixel_id=PX, eventos=[e], cliente="c")
        check(f"evento {rot}: recusa tudo antes de enviar", out.startswith("ERRO") and trecho in out and len(POSTS) == n, out[:100])
    out = await server.enviar_eventos_conversao(pixel_id=PX, eventos=[ev], codigo_teste="TEST123", cliente="c")
    pth, f, _ = POSTS[-1]; d = json.loads(f["data"])[0]
    check("evento enviado: e-mail e telefone com hash normalizado, nada em claro", pth == f"{PX}/events" and d["user_data"]["em"] == [sha("fulano@exemplo.com")]
          and d["user_data"]["ph"] == [sha("5511999998888")] and "fulano" not in f["data"].lower() and "99999" not in f["data"], f["data"][:200])
    check("evento: valor, moeda, fonte WhatsApp (chat) e id de deduplicação", d["custom_data"] == {"value": 150.0, "currency": "BRL"} and d["action_source"] == "chat" and d["event_id"] == "v1"
          and f["test_event_code"] == "TEST123" and "MODO TESTE" in out)
    out = await server.enviar_eventos_conversao(pixel_id=PX, eventos=[ev, dict(ev, evento="Lead", valor=None)], cliente="c")
    check("sem código de teste: avisa que é real e conta lote", "REAIS" not in out and "2 evento(s)" in out and "test_event_code" not in POSTS[-1][1] and "relatórios" in out)
    h = (tmp / "historico/c.jsonl").read_text()
    check("histórico do envio não guarda e-mail nem telefone", "fulano" not in h.lower() and "99999" not in h and "enviar_eventos_conversao" in h)
    check("nenhum dado pessoal nem token em URL", not any(("fulano" in u.lower() or TOKEN in u or PTOKEN in u) for u in URLS))


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
