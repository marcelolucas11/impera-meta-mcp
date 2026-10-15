"""Testa o ciclo de vida do app: uma instância só, fechar com e sem segundo plano, serviço do launchd,
montagem do .app e a interface de permissões, histórico e segundo plano.

Uso: python testes/test_app.py <raiz do repositório>
"""
import json, os, plistlib, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
PY = sys.executable
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright"))
ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))


def instalacao() -> tuple:
    home = Path(tempfile.mkdtemp())
    inst = home / "meta-ads-mcp"
    inst.mkdir()
    for item in ("server.py", "painel.py", "painel.html", "configurar.py", "atualizar.py", "VERSAO", "recursos", "requirements.txt"):
        src = REPO / item
        shutil.copytree(src, inst / item) if src.is_dir() else shutil.copy2(src, inst / item)
    (inst / "clientes").mkdir()
    (inst / "clientes/primos.env").write_text("NOME=Primos\nMETA_ACCESS_TOKEN=TOKEN_TESTE_APP\nMETA_AD_ACCOUNT_ID=111\n")
    return home, inst


def rodar(inst, home, *args, env=None, timeout=60):
    e = dict(os.environ, HOME=str(home), PAINEL_SEM_JANELA="1", **(env or {}))
    return subprocess.run([PY, str(inst / "painel.py"), *args], cwd=str(inst), capture_output=True, text=True, timeout=timeout, env=e)


def estado(inst):
    try:
        return json.loads((inst / ".painel.json").read_text())
    except (OSError, ValueError):
        return None


def vivo(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # processo zumbi conta como encerrado
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


def api(est, rota, corpo=None, metodo="POST"):
    req = urllib.request.Request(f"http://127.0.0.1:{est['porta']}/api/{rota}", method=metodo,
                                 data=None if metodo == "GET" else json.dumps(corpo or {}).encode(),
                                 headers={"X-Painel-Chave": est["chave"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def esperar(cond, segundos=15):
    fim = time.time() + segundos
    while time.time() < fim:
        if cond():
            return True
        time.sleep(0.25)
    return False


# ── 1. uma instância só: o app pede o endereço; clicar de novo reaproveita o mesmo painel ──
home, inst = instalacao()
r1 = rodar(inst, home, "--url"); e1 = estado(inst)
r2 = rodar(inst, home, "--url"); e2 = estado(inst)
check("--url sobe o painel e devolve só o endereço", r1.returncode == 0 and r1.stdout.strip() == f"http://127.0.0.1:{e1['porta']}/#k={e1['chave']}", r1.stdout.strip()[:40])
check("abrir de novo reaproveita o mesmo painel (sem segunda instância)", r2.stdout == r1.stdout and e2["pid"] == e1["pid"])

# ── 2. fechar o app com segundo plano desligado encerra o painel ──
rodar(inst, home, "--ao-fechar-app")
check("fechar com segundo plano desligado: painel encerrado", esperar(lambda: not vivo(e1["pid"])) and estado(inst) is None)

# ── 3. segundo plano ligado: fechar não encerra, e ociosidade também não ──
rodar(inst, home, "--url"); e3 = estado(inst)
res = api(e3, "segundo-plano", {"ativo": True})
env_raiz = (inst / ".env").read_text()
check("ligar segundo plano grava a escolha", res == {"ok": True, "segundo_plano": True} and "SEGUNDO_PLANO=1" in env_raiz)
plist = home / "Library/LaunchAgents/local.impera.meta-mcp.plist"
dados = plistlib.loads(plist.read_bytes()) if plist.exists() else {}
check("serviço do launchd preparado (volta sozinho, com o mesmo painel)", dados.get("ProgramArguments", [])[-2:] == ["--servir", "--reaproveitar"]
      and dados.get("RunAtLoad") is True and dados.get("KeepAlive") == {"SuccessfulExit": False} and dados.get("EnvironmentVariables") == {"IMPERA_LAUNCHD": "1"})
rodar(inst, home, "--ao-fechar-app"); time.sleep(1)
check("fechar com segundo plano ligado: painel continua ativo", vivo(e3["pid"]) and api(e3, "ping") == {"ok": True})
check("estado mostra segundo plano ligado", api(e3, "estado", metodo="GET")["segundo_plano"] is True)
res = api(e3, "segundo-plano", {"ativo": False})
check("desligar segundo plano remove o serviço", not plist.exists() and "SEGUNDO_PLANO=0" in (inst / ".env").read_text())
rodar(inst, home, "--ao-fechar-app")
check("depois de desligar, fechar encerra", esperar(lambda: not vivo(e3["pid"])))

home2, inst2 = instalacao()
(inst2 / ".env").write_text("SEGUNDO_PLANO=1\n")
p = subprocess.Popen([PY, str(inst2 / "painel.py"), "--servir", "--ocioso", "2"], cwd=str(inst2), env=dict(os.environ, HOME=str(home2)),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
esperar(lambda: estado(inst2) is not None); time.sleep(9)
check("em segundo plano, não se encerra por ociosidade", p.poll() is None)
(inst2 / ".env").write_text("SEGUNDO_PLANO=0\n"); time.sleep(8)
check("sem segundo plano, encerra por ociosidade", p.poll() is not None)

# ── 4. sob o launchd: encerrar/atualizar sai com código 75 para o launchd subir o código novo ──
home3, inst3 = instalacao()
p = subprocess.Popen([PY, str(inst3 / "painel.py"), "--servir"], cwd=str(inst3), env=dict(os.environ, HOME=str(home3), IMPERA_LAUNCHD="1"),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
esperar(lambda: estado(inst3) is not None); e4 = estado(inst3)
api(e4, "encerrar")
check("sob o launchd, encerrar = sair com 75 (o launchd reinicia com o código instalado)", p.wait(timeout=15) == 75)
check("sob o launchd, mantém porta e chave para o processo novo", estado(inst3) == e4)
p2 = subprocess.Popen([PY, str(inst3 / "painel.py"), "--servir", "--reaproveitar"], cwd=str(inst3), env=dict(os.environ, HOME=str(home3), IMPERA_LAUNCHD="1"),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
check("o processo que o launchd sobe assume a mesma porta e chave", esperar(lambda: (estado(inst3) or {}).get("pid") == p2.pid)
      and (estado(inst3)["porta"], estado(inst3)["chave"]) == (e4["porta"], e4["chave"]))
p2.terminate(); p2.wait(timeout=10)

# ── 5. montagem do .app (aqui sem osacompile: plano B) e o script do app nativo ──
home5, inst5 = instalacao()
r = rodar(inst5, home5, "--pos-atualizacao", env={"PAINEL_FORCAR_APP": "1"})
app = home5 / "Applications/Impera Meta MCP.app"
info = plistlib.loads((app / "Contents/Info.plist").read_bytes()) if (app / "Contents/Info.plist").exists() else {}
check("sem osacompile: monta o app de plano B (abre no navegador)", "--abrir-navegador" in (app / "Contents/MacOS/abrir").read_text()
      and info.get("CFBundleIdentifier") == "local.impera.meta-mcp" and info.get("CFBundleShortVersionString") == (REPO / "VERSAO").read_text().strip(), r.stderr[-200:])
check("app aceita carregar o painel local (ATS)", info.get("NSAppTransportSecurity") == {"NSAllowsLocalNetworking": True})
outro = home5 / "Applications/Meta Ads MCP.app"; (outro / "Contents/MacOS").mkdir(parents=True)
(outro / "Contents/MacOS/abrir").write_text("#!/bin/bash\necho outro programa\n")
rodar(inst5, home5, "--pos-atualizacao", env={"PAINEL_FORCAR_APP": "1"})
check("app de outro programa com nome antigo não é apagado", outro.exists())
sys.path.insert(0, str(inst5)); import painel  # noqa: E402
script = painel._script_do_app()
check("script do app nativo: caminhos reais, sem marcadores", "__PYTHON__" not in script and "__PAINEL__" not in script and str(inst5 / "painel.py") in script)
check("script do app nativo: reabrir traz a janela, fechar sai, sair avisa o painel",
      "on reopen" in script and "windowWillClose:" in script and "terminate:" in script and "--ao-fechar-app" in script and "--url" in script)
check("script do app nativo: põe a logo no Dock ao abrir", "setApplicationIconImage:" in script and "icone.icns" in script)
montado = Path(tempfile.mkdtemp()) / "x.app"; (montado / "Contents/Resources").mkdir(parents=True)
(montado / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIconName": "applet", "CFBundleIconFile": "applet", "LSUIElement": True}))
painel._ajustar_bundle(montado, None, "icone")
info_m = plistlib.loads((montado / "Contents/Info.plist").read_bytes())
check("bundle do app usa a logo (sem o catálogo de ícone padrão do applet)", "CFBundleIconName" not in info_m and info_m["CFBundleIconFile"] == "icone" and "LSUIElement" not in info_m)
check("script do app nativo: menu Editar para colar o token e Cmd+W para fechar", '"paste:", "v"' in script and '"performClose:", "w"' in script)

# ── 6. interface: permissões, histórico e segundo plano ──
home6, inst6 = instalacao()
(inst6 / "clientes/primos.env").write_text("NOME=Primos\nMETA_ACCESS_TOKEN=TOKEN_TESTE_APP\nMETA_AD_ACCOUNT_ID=111\nPERMISSOES=\n")
(inst6 / "historico").mkdir()
(inst6 / "historico/primos.jsonl").write_text(json.dumps({"quando": "2026-10-15T19:30:00", "ferramenta": "alterar_orcamento", "alvo": "1",
    "parametros": {}, "ok": True, "resultado": "campaign [VENDAS] cpa_600 (1): diário BRL 500.00 -> 350.00"}) + "\n")
rodar(inst6, home6, "--url"); e6 = estado(inst6)
from playwright.sync_api import sync_playwright, expect  # noqa: E402
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
    pg = b.new_page(viewport={"width": 1180, "height": 900})
    erros = []; pg.on("pageerror", lambda e: erros.append(str(e)))
    pg.goto(f"http://127.0.0.1:{e6['porta']}/#k={e6['chave']}")
    expect(pg.locator("#permissoes")).to_contain_text("Criar e duplicar", timeout=15000)
    check("permissões: começa só com leitura", pg.locator("#permissoes [data-perm]:checked").count() == 0)
    expect(pg.locator("#historico")).to_contain_text("diário BRL 500.00 -> 350.00")
    check("histórico de alterações aparece na ficha", "Orçamento" in pg.locator("#historico").inner_text())
    pg.locator("#permissoes label.chave", has_text="Ativar e pausar").click()
    pg.locator("#permissoes label.chave", has_text="Criar e duplicar").click()
    pg.fill("#teto", "1.500,00")
    pg.evaluate("desenharPermissoes()")  # o teste de conexão termina e redesenha a ficha no meio da edição
    check("edição não salva sobrevive a um redesenho", pg.input_value("#teto") == "1.500,00" and pg.locator("#permissoes [data-perm]:checked").count() == 2)
    pg.click("#btn-permissoes")
    expect(pg.locator("#msg-permissoes")).to_contain_text("Permissões salvas")
    env = (inst6 / "clientes/primos.env").read_text()
    check("salvar permissões grava no cliente (e o teto normalizado)", "PERMISSOES=status,criar" in env and "ORCAMENTO_MAX_DIA=1500" in env, env.replace("\n", " | ")[-70:])
    pg.fill("#teto", "abc"); pg.click("#btn-permissoes")
    expect(pg.locator("#msg-permissoes")).to_contain_text("Teto de orçamento inválido")
    check("teto inválido recusado", "ORCAMENTO_MAX_DIA=1500" in (inst6 / "clientes/primos.env").read_text())
    fundo = pg.locator("#chk-fundo")
    check("chave de segundo plano no rodapé", pg.locator("#fundo").inner_text().startswith("Manter em segundo plano"))
    if fundo.is_disabled():  # fora do Mac a chave fica desligada; a API foi testada acima
        check("fora do Mac, a chave de segundo plano aparece desabilitada", sys.platform != "darwin")
    pg.locator("#permissoes").scroll_into_view_if_needed()
    (REPO / "testes/shots").mkdir(exist_ok=True)
    pg.screenshot(path=str(REPO / "testes/shots/permissoes.png"))
    check("sem erros de JavaScript", not erros, "; ".join(erros)[:200])
    b.close()
try:
    api(e6, "encerrar")
except Exception:
    pass
print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
