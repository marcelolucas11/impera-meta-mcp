"""Testa criar_publico_lista e listar_publicos contra uma Graph API simulada.

Uso: python testes/test_publicos.py <raiz do repositório>
Nenhuma chamada real à Meta. Confere permissão, simulação, lotes, sessão de envio, público baseado
em valor, descartes, arquivo sem hash, nome repetido, falha no meio e que nada vai em texto puro.
"""
import asyncio, csv, hashlib, json, os, sys, tempfile
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
server._LOTE_PUBLICO = 3
(tmp / "clientes").mkdir()
TOKEN = "TOKEN_PUBLICOS_PRIMOS_cc33"


def cliente(permissoes):
    (tmp / "clientes/primos.env").write_text(
        f"NOME=primos\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID=111111111111111\nPERMISSOES={permissoes}\n")


def h(x):
    return hashlib.sha256(x.encode()).hexdigest()


def csv_arquivo(nome, cab, linhas):
    p = tmp / nome
    with p.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(cab); w.writerows(linhas)
    return str(p)


CAB = ["email", "phone", "fn", "ln", "ct", "st", "country", "doby", "dobm", "dobd"]
def linha(i, email=True, phone=True, valor=None):
    r = [h(f"e{i}") if email else "", h(f"p{i}") if phone else "", h(f"n{i}"), h(f"s{i}"), "", "", h("br"), "", "", ""]
    return r + ([valor] if valor is not None else [])

# 7 utilizáveis + 1 repetida + 1 sem identificador
lista7 = [linha(i) for i in range(7)] + [linha(0)] + [linha(99, email=False, phone=False)]
ARQ_SIMPLES = csv_arquivo("todos.csv", CAB, lista7)
ARQ_VALOR = csv_arquivo("hilux.csv", CAB + ["value"], [linha(i, valor=str(10 + i) + ".50") for i in range(4)])
ARQ_CRU = csv_arquivo("cru.csv", ["email", "phone"], [[f"pessoa{i}@exemplo.com", f"7199999{i:04d}"] for i in range(30)])

POSTS, ESTADO, EXISTENTES = [], {"falha_lote": 0}, [{"id": "6000000000000001", "name": "Compradores antigos"}]
URLS = []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req):
    URLS.append(str(req.url))
    tok = req.headers.get("authorization", "").removeprefix("Bearer ")
    if tok != TOKEN:
        return err(400, 190, "Invalid OAuth access token")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    if req.method == "GET":
        if p == "act_111111111111111":
            return httpx.Response(200, json={"account_id": "111111111111111", "name": "CA 01 PRIMOS", "currency": "BRL"})
        if p == "act_111111111111111/customaudiences":
            return httpx.Response(200, json={"data": [dict(a, subtype="CUSTOM", approximate_count_lower_bound=1000, approximate_count_upper_bound=1500,
                                                           operation_status={"code": 200, "description": "Normal"},
                                                           delivery_status={"code": 200, "description": "Pronto"}, time_created=1792800000)
                                             for a in EXISTENTES]})
        return err(400, 100, f"Unsupported get request {p}")
    corpo = req.content.decode()
    assert TOKEN not in corpo, "token no corpo do POST"
    form = {k: v[0] for k, v in parse_qs(corpo).items()}
    if p.endswith("/users"):
        n = ESTADO["falha_lote"]
        if n and json.loads(form["session"])["batch_seq"] == n:
            POSTS.append((p, form, "500")); return err(500, 2, "Service temporarily unavailable")
        POSTS.append((p, form, "ok"))
        return httpx.Response(200, json={"audience_id": p.split("/")[0], "num_received": len(json.loads(form["payload"])["data"]), "num_invalid_entries": 0})
    POSTS.append((p, form, "ok"))
    novo = f"6000000000{len(POSTS):06d}"
    EXISTENTES.append({"id": novo, "name": form.get("name")})
    return httpx.Response(200, json={"id": novo})


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


async def main():
    cliente("status")
    n = len(POSTS)
    out = await server.criar_publico_lista(nome="Todos os clientes", arquivo_csv=ARQ_SIMPLES, cliente="primos")
    check("sem permissão de criar: recusa e nada enviado", out.startswith("ERRO") and "não permite criar" in out and len(POSTS) == n, out[:100])

    cliente("status,criar")
    out = await server.criar_publico_lista(nome="Todos os clientes", arquivo_csv=ARQ_SIMPLES, simular=True, cliente="primos")
    check("simulação: mostra contagens e descartes, nada enviado", "SIMULAÇÃO" in out and "9 linhas lidas, 7 utilizáveis" in out
          and "1 sem email/telefone" in out and "1 repetidas" in out and "3 lote(s)" in out and len(POSTS) == n, out.replace("\n", " | ")[:200])

    out = await server.criar_publico_lista(nome="Todos os clientes", arquivo_csv=ARQ_SIMPLES, descricao="base geral", cliente="primos")
    novos = POSTS[n:]
    check("cria o público de lista e envia 3 lotes (3+3+1)", [p.split("/")[-1] for p, _, _ in novos] == ["customaudiences", "users", "users", "users"]
          and [len(json.loads(f["payload"])["data"]) for p, f, _ in novos[1:]] == [3, 3, 1], str([p for p, _, _ in novos]))
    f0 = novos[0][1]
    check("público: tipo CUSTOM, origem informada, sem valor", f0["subtype"] == "CUSTOM" and f0["customer_file_source"] == "USER_PROVIDED_ONLY"
          and f0["name"] == "Todos os clientes" and f0["description"] == "base geral" and "is_value_based" not in f0)
    sess = [json.loads(f["session"]) for _, f, _ in novos[1:]]
    check("sessão: mesmo id, batch_seq 1..3, só o último com last_batch_flag, total estimado",
          len({s["session_id"] for s in sess}) == 1 and [s["batch_seq"] for s in sess] == [1, 2, 3]
          and [s["last_batch_flag"] for s in sess] == [False, False, True] and all(s["estimated_num_total"] == 7 for s in sess))
    pl = json.loads(novos[1][1]["payload"])
    check("schema na ordem do CSV, valores já hasheados passam sem alteração", pl["schema"] == ["EMAIL", "PHONE", "FN", "LN", "CT", "ST", "COUNTRY", "DOBY", "DOBM", "DOBD"]
          and pl["data"][0][0] == h("e0") and pl["data"][0][6] == h("br"))
    check("resposta resume o envio", "OK público" in out and "7 linhas enviadas em 3 lote(s)" in out, out.splitlines()[-1][:140])

    n = len(POSTS)
    out = await server.criar_publico_lista(nome="todos os CLIENTES", arquivo_csv=ARQ_SIMPLES, cliente="primos")
    check("nome repetido (sem diferenciar maiúsculas): recusa sem criar nem enviar", out.startswith("ERRO") and "Já existe um público" in out and len(POSTS) == n, out[:110])

    n = len(POSTS)
    out = await server.criar_publico_lista(nome="Compradores Hilux", arquivo_csv=ARQ_VALOR, cliente="primos")
    novos = POSTS[n:]
    pl = json.loads(novos[1][1]["payload"])
    check("coluna value: público baseado em valor e LOOKALIKE_VALUE no fim do schema", novos[0][1]["is_value_based"] == "true"
          and pl["schema"][-1] == "LOOKALIKE_VALUE" and pl["data"][0][-1] == "10.50" and pl["data"][3 - 3][-1] == "10.50")
    n = len(POSTS)
    out = await server.criar_publico_lista(nome="Compradores Hilux sem valor", arquivo_csv=ARQ_VALOR, baseado_em_valor=False, cliente="primos")
    pl = json.loads(POSTS[n + 1][1]["payload"])
    check("baseado_em_valor=False ignora a coluna value", "is_value_based" not in POSTS[n][1] and "LOOKALIKE_VALUE" not in pl["schema"])
    out = await server.criar_publico_lista(nome="x", arquivo_csv=ARQ_SIMPLES, baseado_em_valor=True, cliente="primos")
    check("baseado_em_valor sem coluna value: recusa", out.startswith("ERRO") and "coluna value" in out)

    n = len(POSTS)
    out = await server.criar_publico_lista(nome="Cru", arquivo_csv=ARQ_CRU, cliente="primos")
    check("arquivo em texto puro (sem hash): recusado, nada sai do Mac", out.startswith("ERRO") and "SHA-256" in out and len(POSTS) == n, out[:120])
    out = await server.criar_publico_lista(nome="Cru", arquivo_csv=str(tmp / "nao_existe.csv"), cliente="primos")
    check("arquivo inexistente: erro claro", out.startswith("ERRO") and ".csv existente" in out)
    out = await server.criar_publico_lista(nome="Cru", arquivo_csv=csv_arquivo("sem_id.csv", ["fn", "ln"], [[h("a"), h("b")]]), cliente="primos")
    check("CSV sem email/phone/madid: explica", out.startswith("ERRO") and "email, phone ou madid" in out)

    n = len(POSTS); ESTADO["falha_lote"] = 2
    out = await server.criar_publico_lista(nome="Falha no meio", arquivo_csv=ARQ_SIMPLES, cliente="primos")
    ESTADO["falha_lote"] = 0
    novos = POSTS[n:]
    check("falha no lote 2: para, não repete e informa o que foi aceito", [s for _, _, s in novos] == ["ok", "ok", "500"]
          and "parou no lote 2 de 3" in out and "3 linhas" in out and "Não repeti nada" in out, out[:200])

    out = await server.listar_publicos(cliente="primos")
    check("listar_publicos: tamanho e status (data Unix como a Meta manda)", "Compradores antigos" in out and "1000" in out and "Pronto" in out and not out.startswith("ERRO"), out.splitlines()[0][:100])
    check("data em texto ISO também funciona", server._data_meta("2026-10-24T01:00:00+0000") == "2026-10-24T01:00" and server._data_meta(None) == "" and server._data_meta("1792800000").startswith("2026-"))

    tudo = "\n".join((tmp / "historico" / a).read_text() for a in os.listdir(tmp / "historico"))
    check("histórico: sem token, sem hashes, com as falhas", TOKEN not in tudo and h("e0") not in tudo and '"ok": false' in tudo and "Todos os clientes" in tudo)
    check("token nunca em URL", not any(TOKEN in u for u in URLS))

asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram"); sys.exit(0 if all(ok) else 1)
