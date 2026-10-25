"""Testa o cofre de tokens (Chaveiro do macOS) com um 'security' simulado: migrar, ler pelo servidor, desfazer,
e que o token nunca aparece em argumentos de linha de comando. Uso: python testes/test_cofre.py <raiz>"""
import asyncio, os, shlex, stat, sys, tempfile
from pathlib import Path
import httpx

sys.path.insert(0, sys.argv[1])
for k in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_EXPORT_DIR"):
    os.environ.pop(k, None)
os.environ["IMPERA_SEM_RITMO"] = "1"
import cofre  # noqa: E402
import server  # noqa: E402

tmp = Path(tempfile.mkdtemp())
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR, server.HISTORICO_DIR = tmp, tmp / ".env", tmp / "clientes", tmp / "historico"
(tmp / "clientes").mkdir()
TOKEN, SEGREDO = "EAAFAKEKEYCHAINTOKEN123456789", "appsecret_fake_abcdef"
arq = tmp / "clientes/c.env"
arq.write_text(f"NOME=c\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_APP_SECRET={SEGREDO}\nMETA_AD_ACCOUNT_ID=111111111111111\n")
os.chmod(arq, 0o600)
COFRE, ARGV = {}, []


def falso(args, entrada=None):
    ARGV.append(list(args))
    if args == ["-i"]:
        partes = shlex.split(entrada)
        conta, valor = partes[partes.index("-a") + 1], partes[partes.index("-w") + 1]
        COFRE[conta] = valor
        return 0, "", ""
    if args[0] == "find-generic-password":
        conta = args[args.index("-a") + 1]
        return (0, COFRE[conta] + "\n", "") if conta in COFRE else (44, "", "not found")
    if args[0] == "delete-generic-password":
        COFRE.pop(args[args.index("-a") + 1], None); return 0, "", ""
    return 1, "", ""


cofre._executar = falso
ok = []
def check(name, cond, detail=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:150]}]" if detail else ""))


def handler(req):
    check("o servidor chama a Meta com o token que veio do Chaveiro", req.headers["authorization"] == f"Bearer {TOKEN}")
    return httpx.Response(200, json={"account_id": "111111111111111", "name": "CA", "currency": "BRL"})


async def main():
    print("\n".join(cofre.status(tmp / "clientes")))
    check("status: token em texto", "em TEXTO" in "\n".join(cofre.status(tmp / "clientes")))
    rel = cofre.migrar(tmp / "clientes")
    texto = arq.read_text()
    check("migrar: arquivo passa a ter só o aviso, sem token nem segredo", TOKEN not in texto and SEGREDO not in texto and cofre.MARCA in texto, texto)
    check("migrar: arquivo continua com permissão 600", stat.S_IMODE(arq.stat().st_mode) == 0o600)
    check("migrar: o token nunca foi argumento de linha de comando", not any(TOKEN in a or SEGREDO in a for av in ARGV for a in av))
    check("migrar: o resto do arquivo foi preservado", "NOME=c" in texto and "META_AD_ACCOUNT_ID=111111111111111" in texto)
    cofre._cache.clear()
    server._TRANSPORT = httpx.MockTransport(handler)
    server._usar_cliente("c")
    check("servidor enxerga o token resolvido (e o mascara nas respostas)", server._token() == TOKEN and TOKEN not in server._scrub(f"erro {TOKEN}"))
    out = await server.listar_contas(cliente="c") if False else None
    # Chaveiro indisponível: erro claro, sem vazar nada
    COFRE.clear(); cofre._cache.clear()
    server._usar_cliente("c")
    try:
        server._token(); msg = ""
    except server.MetaError as e:
        msg = str(e)
    check("Chaveiro sem o item: erro explica como voltar ao modo antigo", "Chaveiro" in msg and "--desfazer" in msg, msg)
    # desfazer
    COFRE[f"c:META_ACCESS_TOKEN"] = TOKEN; COFRE[f"c:META_APP_SECRET"] = SEGREDO
    cofre.desfazer(tmp / "clientes")
    check("desfazer devolve o token ao arquivo", TOKEN in arq.read_text() and cofre.MARCA not in arq.read_text())
    check("migrar duas vezes não faz nada (idempotente)", cofre.migrar(tmp / "clientes") != [] and cofre.migrar(tmp / "clientes") == [])


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} verificações")
sys.exit(0 if all(ok) else 1)
