"""Testa várias contas de anúncios por cliente: conta padrão + contas liberadas no app.

Uso: python testes/test_contas.py <raiz do repositório>
Nenhuma chamada real à Meta. Confere que o Claude só altera a conta padrão e as liberadas,
que 'conta' escolhe onde criar/ler, que objetos de contas não liberadas são recusados sem
enviar nada, e que o app grava e mostra a lista de contas liberadas.
"""
import asyncio, json, os, sys, tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx

sys.path.insert(0, sys.argv[1])
for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_EXPORT_DIR"):
    os.environ.pop(k, None)
import server  # noqa: E402
import painel  # noqa: E402
import configurar  # noqa: E402

tmp = Path(tempfile.mkdtemp())
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR, server.HISTORICO_DIR = tmp, tmp / ".env", tmp / "clientes", tmp / "historico"
configurar.BASE, configurar.ENV, configurar.CLIENTES = tmp, tmp / ".env", tmp / "clientes"
server._BACKOFF = (0, 0, 0)
(tmp / "clientes").mkdir()
TOKEN = "TOKEN_CONTAS_PRIMOS_zz99"
CA1, CA2, CA3 = "111111111111111", "222222222222222", "333333333333333"
NOMES = {CA1: "CA 01 PRIMOS", CA2: "CA 02 PRIMOS", CA3: "CA 03 PRIMOS"}
OBJ = {
    "120000000000000001": {"name": "camp da CA 01", "account_id": CA1, "type": "campaign", "effective_status": "ACTIVE"},
    "120000000000000002": {"name": "camp da CA 02", "account_id": CA2, "type": "campaign", "effective_status": "ACTIVE"},
    "120000000000000003": {"name": "camp da CA 03", "account_id": CA3, "type": "campaign", "effective_status": "ACTIVE"},
}
POSTS, GETS = [], []


def err(status, code, msg):
    return httpx.Response(status, json={"error": {"message": msg, "code": code}})


def handler(req: httpx.Request) -> httpx.Response:
    if req.headers.get("authorization", "").removeprefix("Bearer ") != TOKEN:
        return err(400, 190, "Invalid OAuth access token")
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    q = parse_qs(urlparse(str(req.url)).query)
    if req.method == "GET":
        GETS.append(p)
        if p.startswith("act_") and "/" not in p:
            return httpx.Response(200, json={"account_id": p[4:], "name": NOMES[p[4:]], "currency": "BRL"})
        if p.endswith("/adspixels"):
            return httpx.Response(200, json={"data": [{"id": "77" + p[4:10], "name": "Pixel " + NOMES[p[4:p.index('/')]]}]})
        if p in OBJ:
            o = OBJ[p]
            campos = q.get("fields", [""])[0].split(",")
            body = {"id": p, **{k: o[k] for k in campos if k in o}}
            if q.get("metadata"):
                body["metadata"] = {"type": o["type"]}
            return httpx.Response(200, json=body)
        return err(400, 100, f"Unsupported get request {p}")
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert TOKEN not in req.content.decode(), "token no corpo do POST"
    POSTS.append((p, form))
    return httpx.Response(200, json={"id": "120000000000999001"})


server._TRANSPORT = httpx.MockTransport(handler)
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def cliente(extras=""):
    (tmp / "clientes/primos.env").write_text(
        f"NOME=Primos\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID={CA1}\nPERMISSOES=status,orcamento,editar,criar\nCONTAS_LIBERADAS={extras}\n")


async def main():
    cliente("")
    out = await server.listar_clientes()
    check("listar_clientes mostra a coluna contas_liberadas, com a padrão", "contas_liberadas" in out and CA1 in out)

    n = len(POSTS)
    out = await server.criar_campanha(nome="camp nova", cliente="primos")
    check("sem 'conta': cria na conta padrão", POSTS[n][0] == f"act_{CA1}/campaigns" and "PAUSADA" in out, out[:120])

    n = len(POSTS)
    out = await server.criar_campanha(nome="camp na 02", conta=CA2, cliente="primos")
    check("conta não liberada: recusa, explica e não envia nada",
          out.startswith("ERRO") and "não está liberada" in out and CA1 in out and len(POSTS) == n, out[:160])

    cliente(CA2)
    n = len(POSTS)
    out = await server.criar_campanha(nome="camp na 02", conta=CA2, cliente="primos")
    check("conta liberada: cria na conta pedida (PAUSADA)",
          POSTS[n][0] == f"act_{CA2}/campaigns" and POSTS[n][1].get("status") == "PAUSED" and "PAUSADA" in out, out[:120])
    out = await server.criar_campanha(nome="camp na 02 com act_", conta=f"act_{CA2}", cliente="primos")
    check("aceita o ID com act_", POSTS[-1][0] == f"act_{CA2}/campaigns", out[:100])

    n = len(POSTS)
    out = await server.criar_campanha(nome="camp na 03", conta=CA3, cliente="primos")
    check("liberar uma conta não libera as outras", out.startswith("ERRO") and "não está liberada" in out and len(POSTS) == n)
    out = await server.criar_campanha(nome="camp pelo nome", conta="CA 02 PRIMOS", cliente="primos")
    check("nome de conta não vale para escrever (só ID numérico)", out.startswith("ERRO") and "ID numérico" in out and len(POSTS) == n, out[:120])

    n = len(POSTS)
    out = await server.alterar_status(ids=["120000000000000002"], acao="pausar", cliente="primos")
    check("objeto de conta liberada: altera sem precisar informar a conta",
          POSTS[n][0] == "120000000000000002" and POSTS[n][1] == {"status": "PAUSED"} and "ERRO" not in out, out[:120])
    n = len(POSTS)
    out = await server.alterar_status(ids=["120000000000000003"], acao="pausar", cliente="primos")
    check("objeto de conta NÃO liberada: recusado sem enviar nada",
          "ERRO" in out and "nem uma conta liberada" in out and len(POSTS) == n, out[:160])

    out = await server.listar_pixels(conta=CA2, cliente="primos")
    check("leitura de apoio na conta liberada", "Pixel CA 02 PRIMOS" in out, out[:120])
    out = await server.listar_pixels(conta=CA3, cliente="primos")
    check("leitura de apoio em conta não liberada: recusada", out.startswith("ERRO") and "não está liberada" in out)

    hist = [json.loads(l) for l in (tmp / "historico/primos.jsonl").read_text().splitlines()]
    check("histórico guarda a conta de cada ação", any(h.get("conta") == CA2 for h in hist) and any(h.get("conta") == CA1 for h in hist), str(hist[-1])[:160])

    # app: grava e lê a lista
    painel.op_config("primos", {"contas_liberadas": [CA2, f"act_{CA3}"]})
    check("app grava as contas liberadas no arquivo do cliente", f"CONTAS_LIBERADAS={CA2},{CA3}" in (tmp / "clientes/primos.env").read_text())
    est = painel.op_estado()["clientes"][0]
    check("app mostra as liberadas (sem repetir a padrão)", est["contas_liberadas"] == [CA2, CA3] and est["conta_padrao"] == CA1, str(est["contas_liberadas"]))
    try:
        painel.op_config("primos", {"contas_liberadas": ["abc"]}); recusou = False
    except painel.Erro:
        recusou = True
    check("app recusa ID inválido", recusou)
    painel.op_config("primos", {"contas_liberadas": []})
    check("desmarcar todas limpa a lista e preserva o token", "CONTAS_LIBERADAS=\n" in (tmp / "clientes/primos.env").read_text()
          and TOKEN in (tmp / "clientes/primos.env").read_text())
    n = len(POSTS)
    out = await server.criar_campanha(nome="depois de desmarcar", conta=CA2, cliente="primos")
    check("depois de desmarcar, a conta volta a ser recusada", out.startswith("ERRO") and len(POSTS) == n)

asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações passaram"); sys.exit(0 if all(ok) else 1)
