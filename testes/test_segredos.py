"""Procura token da Meta, chaves de API e chaves privadas em todos os arquivos do repositório, inclusive na pasta testes/.
Roda junto com os outros testes (bash testes/rodar.sh), então pega o vazamento antes do envio, e não só no GitHub Actions.
Os únicos valores parecidos com segredo que podem existir são os falsos listados em FALSOS_PERMITIDOS.
Uso: python testes/test_segredos.py <raiz>"""
import re, subprocess, sys
from pathlib import Path

raiz = Path(sys.argv[1]).resolve()

PADROES = {
    "token da Meta": re.compile(r"EAA[A-Za-z0-9]{20,}"),
    "token do GitHub": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "chave da Anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "chave da OpenAI": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "chave da AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "chave do Google": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "token do Slack": re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    "chave privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "token em URL": re.compile(r"access_token=[A-Za-z0-9]{20,}"),
    "segredo de app da Meta": re.compile(r"(?i)app_?secret[\"'\s:=]{1,6}[a-f0-9]{32}\b"),
}
# Valores falsos usados pelos testes. Nada fora desta lista pode parecer segredo.
FALSOS_PERMITIDOS = [re.compile(r"^EAAFAKE[A-Z0-9]*$")]

falhas = 0


def check(nome, ok, detalhe=""):
    global falhas
    print(("ok   " if ok else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe and not ok else ""))
    falhas += 0 if ok else 1


def achados(texto):
    for nome, rx in PADROES.items():
        for m in rx.finditer(texto):
            if not any(f.match(m.group(0)) for f in FALSOS_PERMITIDOS):
                yield nome, m.group(0)


def arquivos():
    try:
        saida = subprocess.run(["git", "-C", str(raiz), "ls-files", "-z"], capture_output=True, check=True).stdout
        lista = [raiz / p for p in saida.decode().split("\0") if p]
        if lista:
            return lista
    except (OSError, subprocess.CalledProcessError):
        pass
    fora = {".git", ".venv", "dist", "shots", "__pycache__", "clientes", "historico", "relatorios", ".versoes-anteriores"}
    return [p for p in raiz.rglob("*") if p.is_file() and not fora & set(p.relative_to(raiz).parts)]


# O detector precisa funcionar: valores montados na hora (nunca escritos no arquivo) têm de ser pegos.
falso_real = "EAA" + "Bq7Zx" * 10
check("detector pega token da Meta que não está na lista de falsos", any(n == "token da Meta" for n, _ in achados(falso_real)))
check("detector pega chave da Anthropic", bool(list(achados("sk-ant-" + "a1B2" * 10))))
check("detector aceita o token falso dos testes", not list(achados("EAAFAKEKEYCHAINTOKEN123456789")))

encontrados = []
lista = arquivos()
for arq in lista:
    try:
        dados = arq.read_bytes()
    except OSError:
        continue
    if b"\x00" in dados[:8000]:
        continue  # binário (ícone)
    for nome, valor in achados(dados.decode("utf-8", errors="replace")):
        encontrados.append(f"{arq.relative_to(raiz)}: {nome} ({valor[:6]}…)")

check(f"nenhum segredo nos {len(lista)} arquivos do repositório (inclusive testes/)", not encontrados, "; ".join(encontrados[:10]))
check(".gitignore protege dados do usuário", all(
    linha in (raiz / ".gitignore").read_text().splitlines() for linha in (".env", "clientes/", "historico/", "relatorios/", ".venv/")))

print(f"\n{'TUDO OK' if not falhas else f'{falhas} FALHA(S)'}")
sys.exit(1 if falhas else 0)
