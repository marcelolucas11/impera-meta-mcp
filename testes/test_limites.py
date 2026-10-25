"""Testa a proteção contra bloqueio (protecao.py) contra uma Graph API simulada: não insistir em limite,
disjuntor por conta e por token, sinal de abuso, cabeçalhos de uso, ritmo de escrita, status_limites.
Uso: python testes/test_limites.py <raiz do repositório>"""
import asyncio, json, os, stat, sys, tempfile
from pathlib import Path
from urllib.parse import urlparse
import httpx

sys.path.insert(0, sys.argv[1])
for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_EXPORT_DIR"):
    os.environ.pop(k, None)
import server  # noqa: E402
import protecao  # noqa: E402

tmp = Path(tempfile.mkdtemp())
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR, server.HISTORICO_DIR = tmp, tmp / ".env", tmp / "clientes", tmp / "historico"
server._BACKOFF = (0, 0, 0)
(tmp / "clientes").mkdir()
TA, TB = "TOKEN_LIM_A_9f8e7d", "TOKEN_LIM_B_1a2b3c"
A1, A2, B1 = "111111111111111", "222222222222222", "333333333333333"
(tmp / "clientes/a.env").write_text(f"NOME=a\nMETA_ACCESS_TOKEN={TA}\nMETA_AD_ACCOUNT_ID={A1}\nCONTAS_LIBERADAS={A2}\nPERMISSOES=status,orcamento,editar,criar\n")
(tmp / "clientes/b.env").write_text(f"NOME=b\nMETA_ACCESS_TOKEN={TB}\nMETA_AD_ACCOUNT_ID={B1}\nPERMISSOES=status,orcamento,editar,criar\n")

RELOGIO = [1_000_000.0]
protecao.agora = lambda: RELOGIO[0]
CHAMADAS = []   # (método, caminho)
MODO = {}       # caminho(prefixo) -> resposta


def handler(req: httpx.Request) -> httpx.Response:
    p = urlparse(str(req.url)).path.split("/", 2)[2]
    CHAMADAS.append((req.method, p, req.headers["authorization"][7:]))
    for pref, fn in MODO.items():
        if p.startswith(pref):
            return fn(req)
    return httpx.Response(200, json={"id": "1", "name": "x"})


def limite(code=17, sub=None, headers=None):
    e = {"message": "User request limit reached", "code": code}
    if sub: e["error_subcode"] = sub
    return lambda req: httpx.Response(400, json={"error": e}, headers=headers or {})


server._TRANSPORT = httpx.MockTransport(handler)
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:160]}]" if detail else ""))
def n(token=None): return len([c for c in CHAMADAS if token is None or c[2] == token])
def zerar(): CHAMADAS.clear(); MODO.clear()


async def get(cli, path):
    server._usar_cliente(cli)
    try:
        async with server._client() as c:
            return await server._get(c, path, {"fields": "id"})
    except server.MetaError as e:
        return e

async def post(cli, path, conta=None):
    server._usar_cliente(cli)
    try:
        async with server._client() as c:
            return await server._post(c, path, {"name": "x"})
    except server.MetaError as e:
        return e


async def main():
    # 1) limite de uso: uma tentativa só, sem insistir
    MODO[f"act_{A1}"] = limite(17)
    r = await get("a", f"act_{A1}/insights")
    check("limite de uso: uma única chamada (não insiste)", n() == 1 and isinstance(r, server.MetaError) and "Não vou insistir" in str(r), r)
    r = await get("a", f"act_{A1}/insights"); r = await get("a", f"act_{A1}/insights")
    check("3º limite em 5 min abre o disjuntor de 15 min", n() == 3 and "Parei de chamar a Meta até" in str(r), r)
    antes = n()
    r = await get("a", f"act_{A1}/campaigns")
    check("com o disjuntor aberto nem chama a Meta", n() == antes and "Proteção ativa" in str(r) and "até" in str(r), r)
    r = await get("a", f"act_{A2}/campaigns")
    check("outra conta do mesmo cliente segue livre", n() == antes + 1 and not isinstance(r, server.MetaError), r)
    r = await get("b", f"act_{B1}/campaigns")
    check("outro cliente (outro token) não é afetado", not isinstance(r, server.MetaError) and CHAMADAS[-1][2] == TB, r)
    r = await post("a", f"act_{A1}/campaigns")
    check("escrita na conta bloqueada também é recusada sem chamar", n() == antes + 2 and isinstance(r, server.MetaError) and "Proteção ativa" in str(r), r)
    RELOGIO[0] += 16 * 60
    MODO.clear()
    r = await get("a", f"act_{A1}/campaigns")
    check("passado o prazo volta a chamar", not isinstance(r, server.MetaError), r)

    # 2) sinal de abuso 613/1996: 60 min, persiste em disco, sem token no arquivo
    zerar()
    MODO[f"act_{A1}"] = limite(613, 1996)
    r = await get("a", f"act_{A1}/insights")
    check("613/1996: uma chamada e bloqueio de ~60 min", n() == 1 and "60 min" in str(r).replace("cerca de 60", "60 min") or "abuso" in str(r), r)
    arq = tmp / ".limites.json"
    texto = arq.read_text() if arq.exists() else ""
    check("estado gravado em disco, permissão 600, sem token", arq.exists() and stat.S_IMODE(arq.stat().st_mode) == 0o600 and TA not in texto and TB not in texto, oct(stat.S_IMODE(arq.stat().st_mode)) if arq.exists() else "sem arquivo")
    protecao._cache_estado.update(mtime=None, dados={}, caminho=None); protecao._memoria.clear(); protecao._limitados.clear()
    zerar()
    r = await get("a", f"act_{A1}/insights")
    check("reiniciar o servidor não apaga o bloqueio", n() == 0 and "Proteção ativa" in str(r), r)
    RELOGIO[0] += 61 * 60

    # 3) código 368: bloqueio do token inteiro (todas as contas)
    zerar()
    MODO[f"act_{A1}"] = limite(368)
    await get("a", f"act_{A1}/campaigns")
    r = await get("a", f"act_{A2}/campaigns")
    check("368 bloqueia o token inteiro por 30 min", "Proteção ativa" in str(r) and "este token" in str(r), r)
    r = await get("b", f"act_{B1}/campaigns")
    check("368 de um cliente não bloqueia o outro", not isinstance(r, server.MetaError), r)
    RELOGIO[0] += 31 * 60

    # 4) a Meta diz quanto esperar (cabeçalho)
    zerar()
    buc = json.dumps({A2: [{"type": "ads_insights", "call_count": 100, "total_cputime": 20, "total_time": 30, "estimated_time_to_regain_access": 7}]})
    MODO[f"act_{A2}"] = limite(80000, headers={"x-business-use-case-usage": buc})
    r = await get("a", f"act_{A2}/insights")
    b = protecao.bloqueio(tmp, TA, f"act_{A2}")
    check("espera estimada da Meta vira o prazo do bloqueio (7 min)", b is not None and b["minutos"] == 7, b)
    RELOGIO[0] += 8 * 60

    # 5) uso alto em resposta de sucesso: degraus e status_limites
    zerar()
    MODO[f"act_{A1}"] = lambda req: httpx.Response(200, json={"data": []}, headers={"x-app-usage": json.dumps({"call_count": 85, "total_time": 10, "total_cputime": 5})})
    await get("a", f"act_{A1}/campaigns")
    check("uso 85% é lido dos cabeçalhos", protecao.uso_atual(TA, f"act_{A1}").get("pct") == 85)
    check("degraus: 85% → 3s, 95% → 8s, 50% → 0", protecao.degrau(85) == 3.0 and protecao.degrau(95) == 8.0 and protecao.degrau(50) == 0)
    server._usar_cliente("a")
    out = await server.status_limites(cliente="a")
    check("status_limites mostra o uso e nenhum bloqueio", "Uso act_" in out and "85%" in out and "Nenhum bloqueio ativo" in out and TA not in out, out)
    MODO[f"act_{A2}"] = lambda req: httpx.Response(200, json={"data": []}, headers={"x-ad-account-usage": json.dumps({"acc_id_util_pct": 100})})
    await get("a", f"act_{A2}/campaigns")
    r = await get("a", f"act_{A2}/campaigns")
    check("uso em 100% pausa as chamadas daquela conta", "Proteção ativa" in str(r), r)
    out = await server.status_limites(cliente="a")
    check("status_limites mostra o bloqueio com a hora", "BLOQUEADO (conta act_" in out and "até " in out, out)
    RELOGIO[0] += 5 * 60

    # 6) escrita: limite no POST não é repetido e diz que nada foi alterado
    zerar()
    MODO[f"act_{A1}/campaigns"] = limite(17)
    r = await post("a", f"act_{A1}/campaigns")
    check("POST com limite: uma chamada, 'Nada foi alterado'", n() == 1 and "Nada foi alterado" in str(r), r)
    RELOGIO[0] += 20 * 60

    # 7) ritmo de escrita: duas escritas seguidas na mesma conta são espaçadas
    zerar()
    dormidos = []
    antigo_dormir, antigo_ritmo = protecao.dormir, protecao.RITMO
    async def espiar(s): dormidos.append(s)
    protecao.dormir = espiar
    await post("a", f"act_{A1}/campaigns"); await post("a", f"act_{A1}/campaigns"); await post("a", f"act_{A2}/campaigns")
    protecao.dormir = antigo_dormir
    check("escrita seguida na mesma conta espera ~1s e em outra conta não", len(dormidos) == 1 and 0.5 <= dormidos[0] <= 1.0, dormidos)

    # 7b) fila de escrita compartilhada entre processos (arquivo com trava)
    import subprocess
    cod = ("import sys,json;sys.path.insert(0,sys.argv[1]);import protecao;from pathlib import Path;"
           "print(protecao._reservar_vaga(Path(sys.argv[2]),'k:act_1'))")
    saidas = [subprocess.Popen([sys.executable, "-c", cod, sys.argv[1], str(tmp)], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    vagas = sorted(float(p.communicate()[0]) for p in saidas)
    check("4 processos reservam vagas de escrita 1s depois uma da outra", all(b - a >= 0.99 for a, b in zip(vagas, vagas[1:])), [round(v - vagas[0], 2) for v in vagas])

    # 7c) o app (painel) mostra os bloqueios ativos
    import painel
    MODO.clear(); MODO[f"act_{A1}"] = limite(613, 1996)
    await get("a", f"act_{A1}/insights")
    bl = painel._limites({"_id": "a", "META_ACCESS_TOKEN": TA})
    check("painel recebe o bloqueio ativo do cliente (e nada de outro cliente)", len(bl) == 1 and bl[0]["conta"] == f"act_{A1}" and not painel._limites({"_id": "b", "META_ACCESS_TOKEN": TB}), bl)
    protecao.fechar(tmp, TA, f"act_{A1}"); zerar()

    # 8) leituras simultâneas por conta ficam limitadas
    ativos = {"n": 0, "max": 0}
    async def lenta(req_):
        return None
    zerar()
    def contar(req):
        return httpx.Response(200, json={"data": []})
    MODO[f"act_{A1}"] = contar
    await asyncio.gather(*[get("a", f"act_{A1}/ads") for _ in range(20)])
    check("20 leituras simultâneas completam sem erro", n() == 20)


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
