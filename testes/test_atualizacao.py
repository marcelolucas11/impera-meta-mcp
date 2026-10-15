"""Testa a atualização: nada do usuário pode ser perdido, e pacotes ruins são recusados.

Uso: python testes/test_atualizacao.py <raiz do repositório>
Sobe um "GitHub" falso em 127.0.0.1 com uma release 9.9.9 e confere:
  - atualizar.py direto (checksum errado, zip malicioso, atualização boa, já atualizado);
  - o fluxo completo pelo app num navegador (botão Atualizar agora, reinício na mesma janela);
  - o instalador por cima de uma instalação existente e a instalação por uma linha (instalar-mac.sh).
"""
import hashlib, http.server, importlib, io, json, os, shutil, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
PY = sys.executable
PROGRAMA = ["server.py", "painel.py", "painel.html", "configurar.py", "atualizar.py", "instalar.sh",
            "Instalar Impera Meta MCP.command", "requirements.txt", ".env.example", "VERSAO", "README.md",
            "NOVIDADES.md", "recursos"]
TOKEN = "TOKEN_DO_CLIENTE_NAO_PODE_SUMIR_123"
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def copiar_programa(destino: Path, versao: str):
    destino.mkdir(parents=True, exist_ok=True)
    for item in PROGRAMA:
        src = REPO / item
        (shutil.copytree(src, destino / item, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, destino / item))
    (destino / "VERSAO").write_text(versao + "\n")


def instalacao_com_dados(base: Path, versao="4.1.0") -> Path:
    inst = base / "meta-ads-mcp"
    copiar_programa(inst, versao)
    (inst / "clientes").mkdir()
    (inst / "clientes/primos.env").write_text(f"NOME=Primos\nMETA_ACCESS_TOKEN={TOKEN}\nMETA_AD_ACCOUNT_ID=111\n")
    os.chmod(inst / "clientes/primos.env", 0o600)
    (inst / ".env").write_text("META_API_VERSION=v26.0\nMETA_EXPORT_DIR=\n")
    (inst / "relatorios/primos").mkdir(parents=True)
    (inst / "relatorios/primos/relatorio-antigo.xlsx").write_bytes(b"planilha antiga")
    (inst / "painel.log").write_text("log antigo\n")
    return inst


def dados_do_usuario(inst: Path) -> dict:
    out = {}
    for rel in ("clientes/primos.env", ".env", "relatorios/primos/relatorio-antigo.xlsx", "painel.log"):
        p = inst / rel
        out[rel] = p.read_bytes() if p.exists() else None
    return out


def pacote(versao: str, extras: dict = None) -> bytes:
    """Zip no formato da release, com a pasta raiz impera-meta-mcp/."""
    tmp = Path(tempfile.mkdtemp())
    raiz = tmp / "impera-meta-mcp"
    copiar_programa(raiz, versao)
    html = (raiz / "painel.html").read_text().replace("</body>", f"<!-- versao-de-teste-{versao} -->\n</body>")
    (raiz / "painel.html").write_text(html)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(raiz.rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo(str(p.relative_to(tmp)))
                info.external_attr = (0o100755 if p.suffix in (".sh", ".command") else 0o100644) << 16
                z.writestr(info, p.read_bytes())
        for nome, conteudo in (extras or {}).items():
            z.writestr(nome, conteudo)
    shutil.rmtree(tmp)
    return buf.getvalue()


# ── GitHub falso ──
SERVIDO = {}


class GitHubFalso(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        corpo = SERVIDO.get(self.path.split("?")[0])
        if corpo is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(corpo))); self.end_headers(); self.wfile.write(corpo)


gh = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GitHubFalso)
threading.Thread(target=gh.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{gh.server_address[1]}"


def publicar(zip_bytes: bytes, versao: str, sha: str = None):
    SERVIDO["/pkg/impera-meta-mcp.zip"] = zip_bytes
    SERVIDO["/pkg/impera-meta-mcp.zip.sha256"] = f"{sha or hashlib.sha256(zip_bytes).hexdigest()}  impera-meta-mcp.zip\n".encode()
    SERVIDO["/latest"] = json.dumps({
        "tag_name": f"v{versao}", "published_at": "2026-10-20T12:00:00Z", "html_url": URL + "/releases",
        "body": f"- Novidade da versão {versao}\n- Clientes e tokens preservados",
        "assets": [{"name": "impera-meta-mcp.zip", "browser_download_url": URL + "/pkg/impera-meta-mcp.zip"},
                   {"name": "impera-meta-mcp.zip.sha256", "browser_download_url": URL + "/pkg/impera-meta-mcp.zip.sha256"}],
    }).encode()


os.environ.update(IMPERA_URL_RELEASES=URL + "/latest", IMPERA_PULAR_DEPENDENCIAS="1", PAINEL_SEM_JANELA="1")
os.environ["NO_PROXY"] = os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

# ── 1. atualizar.py direto ──
base = Path(tempfile.mkdtemp()); os.environ["HOME"] = str(base)
inst = instalacao_com_dados(base)
antes = dados_do_usuario(inst)
sys.path.insert(0, str(inst)); atualizar = importlib.import_module("atualizar")
assert atualizar.BASE == inst

bom = pacote("9.9.9", extras={"impera-meta-mcp/clientes/primos.env": "META_ACCESS_TOKEN=TOKEN_TROCADO_PELO_PACOTE\n",
                              "impera-meta-mcp/.env": "SOBRESCRITO=1\n",
                              "impera-meta-mcp/relatorios/primos/relatorio-antigo.xlsx": "apagado"})
publicar(bom, "9.9.9", sha="0" * 64)
info = atualizar.buscar()
check("buscar: encontra a 9.9.9 com notas", info["disponivel"] and info["nova"] == "9.9.9" and "Novidade" in info["notas"], f"atual {info['atual']}")
try:
    atualizar.atualizar(); check("checksum errado: recusado", False)
except atualizar.ErroAtualizacao as e:
    check("checksum errado: recusado e nada mudou", "checksum" in str(e) and atualizar.versao_local() == "4.1.0" and dados_do_usuario(inst) == antes)

malicioso = pacote("9.9.9", extras={"impera-meta-mcp/../../fora-da-pasta.txt": "invasão"})
publicar(malicioso, "9.9.9")
try:
    atualizar.atualizar(); check("zip com '..': recusado", False)
except atualizar.ErroAtualizacao as e:
    check("zip com '..': recusado, nada escrito fora", "inválidos" in str(e) and not (base / "fora-da-pasta.txt").exists() and atualizar.versao_local() == "4.1.0")

publicar(bom, "9.9.9")
res = atualizar.atualizar()
check("atualização boa: 4.1.0 -> 9.9.9", res.get("atualizado") and res["de"] == "4.1.0" and res["para"] == "9.9.9" and atualizar.versao_local() == "9.9.9", str({k: res.get(k) for k in ("de", "para", "arquivos")}))
check("código novo instalado", "versao-de-teste-9.9.9" in (inst / "painel.html").read_text())
depois = dados_do_usuario(inst)
check("token do cliente intacto (o pacote tentou trocar)", depois["clientes/primos.env"] == antes["clientes/primos.env"] and TOKEN.encode() in depois["clientes/primos.env"])
check("ajustes (.env), relatórios e log intactos", all(depois[k] == antes[k] for k in (".env", "relatorios/primos/relatorio-antigo.xlsx", "painel.log")))
check("permissão 600 do arquivo do cliente mantida", oct((inst / "clientes/primos.env").stat().st_mode & 0o777) == "0o600")
bk = list((inst / ".versoes-anteriores").glob("4.1.0-*"))
check("versão anterior guardada", len(bk) == 1 and "versao-de-teste" not in (bk[0] / "painel.html").read_text() and (bk[0] / "VERSAO").read_text().strip() == "4.1.0")
check("sem arquivos temporários sobrando", not list(inst.rglob("*.novo")))
check("já atualizado: nada a fazer", atualizar.atualizar() == {"atualizado": False, "atual": "9.9.9", "mensagem": "Você já está na versão mais nova."})
del sys.modules["atualizar"]; sys.path.remove(str(inst))

# ── 2. fluxo completo pelo app ──
base2 = Path(tempfile.mkdtemp()); os.environ["HOME"] = str(base2)
inst2 = instalacao_com_dados(base2)
antes2 = dados_do_usuario(inst2)
publicar(pacote("9.9.9"), "9.9.9")
srv = subprocess.Popen([PY, str(inst2 / "painel.py"), "--servir"], cwd=str(inst2), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    if (inst2 / ".painel.json").exists(): break
    time.sleep(0.2)
est = json.loads((inst2 / ".painel.json").read_text())
from playwright.sync_api import sync_playwright, expect  # noqa: E402
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
    pg = b.new_page(viewport={"width": 1180, "height": 840})
    erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.goto(f"http://127.0.0.1:{est['porta']}/#k={est['chave']}")
    expect(pg.locator("#versao")).to_contain_text("Versão 4.1.0")
    expect(pg.locator("#versao")).to_contain_text("Versão 9.9.9 disponível", timeout=15000)
    check("app avisa sozinho que há versão nova", True)
    pg.locator("#versao summary").click()
    check("app mostra o que mudou", "Novidade da versão 9.9.9" in pg.locator("#versao .notas").inner_text())
    pg.screenshot(path=str(REPO / "testes/shots/atualizacao-disponivel.png")) if (REPO / "testes/shots").exists() else None
    pg.click("#btn-atualizar")
    expect(pg.locator("#versao")).to_contain_text("Atualizado para a versão 9.9.9", timeout=60000)
    check("atualizou e reabriu sozinho na mesma janela", "versao-de-teste-9.9.9" in pg.content())
    expect(pg.locator(".bilhete")).to_have_count(1)
    check("cliente continua na lista depois de atualizar", pg.locator(".bilhete .nome").inner_text() == "Primos")
    check("aviso para reiniciar o Claude", "Cmd+Q" in pg.locator("#versao").inner_text())
    if (REPO / "testes/shots").exists():
        pg.screenshot(path=str(REPO / "testes/shots/atualizacao-concluida.png"))
    check("sem erros de JavaScript", not erros, "; ".join(erros)[:200])
    b.close()
est2 = json.loads((inst2 / ".painel.json").read_text())
check("reiniciou na mesma porta e com a mesma chave", est2["porta"] == est["porta"] and est2["chave"] == est["chave"] and est2["pid"] != est["pid"],
      f"antes porta {est['porta']} pid {est['pid']}, depois porta {est2['porta']} pid {est2['pid']}, chave igual {est2['chave'] == est['chave']}")
check("dados do usuário intactos depois do fluxo pelo app", dados_do_usuario(inst2) == antes2)
import urllib.request  # noqa: E402
req = urllib.request.Request(f"http://127.0.0.1:{est['porta']}/api/encerrar", data=b"{}", method="POST", headers={"X-Painel-Chave": est["chave"]})
urllib.request.urlopen(req, timeout=5); srv.wait(timeout=10)

# ── 3. instalador por cima de uma instalação existente e instalação por uma linha ──
base3 = Path(tempfile.mkdtemp())
inst3 = instalacao_com_dados(base3, versao="4.0.0")
(inst3 / ".venv").symlink_to(REPO / ".venv")  # ambiente Python já existente
antes3 = dados_do_usuario(inst3)
env = dict(os.environ, HOME=str(base3), PAINEL_SEM_JANELA="1")
z = pacote("9.9.9"); publicar(z, "9.9.9")
r = subprocess.run(["bash", str(REPO / "instalar-mac.sh")], env=dict(env, IMPERA_URL_PACOTE=URL + "/pkg/impera-meta-mcp.zip"),
                   capture_output=True, text=True, timeout=600)
saida = (r.stdout + r.stderr)
check("instalação por uma linha: baixou, conferiu e instalou", r.returncode == 0 and (inst3 / "VERSAO").read_text().strip() == "9.9.9", saida.strip().splitlines()[-1][:120] if saida.strip() else "")
check("instalação por cima: dados do usuário intactos", dados_do_usuario(inst3) == antes3)
check("instalação por cima: não copiou pasta de clientes de fora", not (inst3 / "impera-meta-mcp").exists())
SERVIDO["/pkg/impera-meta-mcp.zip.sha256"] = b"0" * 64 + b"  impera-meta-mcp.zip\n"
(inst3 / "VERSAO").write_text("4.0.0\n")
r = subprocess.run(["bash", str(REPO / "instalar-mac.sh")], env=dict(env, IMPERA_URL_PACOTE=URL + "/pkg/impera-meta-mcp.zip"),
                   capture_output=True, text=True, timeout=120)
check("instalação por uma linha com checksum errado: nada instalado", r.returncode != 0 and "não confere" in r.stdout and (inst3 / "VERSAO").read_text().strip() == "4.0.0")
for est_path in (inst3 / ".painel.json",):
    try:
        e3 = json.loads(est_path.read_text())
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{e3['porta']}/api/encerrar", data=b"{}", method="POST",
                                                      headers={"X-Painel-Chave": e3["chave"]}), timeout=5)
    except Exception:
        pass

gh.shutdown()
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
