import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
from datetime import date, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

py, proj, S = sys.argv[1], sys.argv[2], Path(sys.argv[3])
tmp = Path(tempfile.mkdtemp()); (tmp / "Claude").mkdir(); PORTA = 8791
srv = subprocess.Popen([py, str(S / "wrap_painel.py"), proj, str(tmp), str(PORTA)], stderr=subprocess.PIPE, text=True)
for _ in range(60):
    if (tmp / ".painel.json").exists(): break
    time.sleep(0.2)
else: raise SystemExit("painel não subiu: " + srv.stderr.read()[-2000:])
est = json.loads((tmp / ".painel.json").read_text()); CHAVE = est["chave"]; BASEURL = f"http://127.0.0.1:{PORTA}"
ok = []
def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{detalhe}]" if detalhe else ""))
def http(metodo, caminho, corpo=None, **headers):
    req = urllib.request.Request(BASEURL + caminho, data=json.dumps(corpo).encode() if corpo is not None else None, method=metodo, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

# ── segurança da API local ──
check("API sem chave: 401", http("GET", "/api/estado")[0] == 401)
check("API com chave errada: 401", http("GET", "/api/estado", **{"X-Painel-Chave": "x" * 43})[0] == 401)
check("Host forjado (DNS rebinding): 403", http("GET", "/api/estado", **{"X-Painel-Chave": CHAVE, "Host": "evil.example:8791"})[0] == 403)
check("Origin de outro site: 403", http("POST", "/api/ping", {}, **{"X-Painel-Chave": CHAVE, "Origin": "https://evil.example"})[0] == 403)
check("página não traz CORS liberado", "access-control" not in str(urllib.request.urlopen(BASEURL + "/").headers).lower())
check("recursos: travessia de pasta bloqueada", http("GET", "/recursos/..%2Fpainel.py")[0] == 404 and http("GET", "/recursos/../.painel.json")[0] in (403, 404))
check("estado do painel com permissão 600", oct((tmp / ".painel.json").stat().st_mode & 0o777) == "0o600")

hoje = date.today(); ontem = hoje - timedelta(days=1)
shots = S / "shots"; shots.mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or None)
    ctx = b.new_context(viewport={"width": 1180, "height": 840}, device_scale_factor=2, permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page(); erros = []
    pg.on("pageerror", lambda e: erros.append(str(e))); pg.on("console", lambda m: erros.append(m.text) if m.type == "error" else None)

    pg.goto(BASEURL + "/"); expect(pg.get_by_role("heading", name="Abra pelo app")).to_be_visible()
    check("sem a chave na URL: orienta a abrir pelo app", True)
    erros.clear()  # o 401 acima é esperado

    pg.goto(BASEURL + "/#k=" + CHAVE); pg.reload()
    expect(pg.get_by_role("heading", name="Cadastre o primeiro cliente")).to_be_visible()
    pg.screenshot(path=str(shots / "1-vazio.png"))
    check("primeira abertura: convite para cadastrar", pg.locator("#contagem").inner_text() == "nenhum cliente")

    pg.fill("#nome", "Primos Premiações"); pg.fill("#token", "tem espaço no token"); pg.click("#btn-salvar")
    expect(pg.locator("#msg-novo")).to_contain_text("não parece um token")
    check("token com espaços: recusado com explicação", not (tmp / "clientes").exists() or not list((tmp / "clientes").glob("*.env")))

    pg.fill("#token", "TOKPRIMOS"); pg.click("#btn-salvar")
    expect(pg.locator("#estado")).to_have_text("Conectado", timeout=15000)
    expect(pg.locator("#sub")).to_contain_text("PRIMOS BM01 CA 01 (111111111111111)")
    expect(pg.locator(".numeros")).to_be_visible(timeout=15000)
    check("cadastro: conectou e a única conta virou padrão", "META_AD_ACCOUNT_ID=111111111111111" in (tmp / "clientes/primos-premiacoes.env").read_text())
    check("arquivo do cliente com permissão 600", oct((tmp / "clientes/primos-premiacoes.env").stat().st_mode & 0o777) == "0o600")
    nums = pg.locator(".numeros dd").all_inner_texts(); check("últimos 7 dias: quatro números da conta", len(nums) == 4 and nums[0].startswith("R$") and "," in nums[2], " | ".join(nums))
    check("dono do token e permissões na ficha", "relatorios-primos" in pg.locator("#conexao").inner_text() and "ads_read" in pg.locator("#conexao").inner_text())

    pg.click("#btn-novo"); pg.fill("#nome", "Dona Co"); pg.fill("#token", "TOKDONA"); pg.click("#btn-salvar")
    expect(pg.locator("#estado")).to_have_text("Conectado", timeout=15000)
    expect(pg.locator("#sub")).to_have_text("Sem conta padrão")
    check("duas contas: não escolhe sozinho", pg.locator("#btn-exportar").is_disabled() and "Escolha a conta padrão" in pg.locator("#resumo").inner_text())
    pg.select_option("#sel-conta", "687018494504975")
    expect(pg.locator("#msg-conexao")).to_contain_text("Conta padrão salva"); expect(pg.locator(".numeros")).to_be_visible(timeout=15000)
    check("conta padrão escolhida na lista e gravada", "META_AD_ACCOUNT_ID=687018494504975" in (tmp / "clientes/dona-co.env").read_text() and "DONA CO BM01 CA 02" in pg.locator(".bilhete[aria-current=true] .conta").inner_text())

    pg.click("#btn-novo"); pg.fill("#nome", "Cliente Ruim"); pg.fill("#token", "TOKRUIM"); pg.click("#btn-salvar")
    expect(pg.locator("#estado")).to_have_text("Com erro", timeout=15000)
    check("token recusado pela Meta: erro legível, sem o token", "recusou o token do cliente 'cliente-ruim'" in pg.locator("#erro-teste").inner_text() and "TOKRUIM" not in pg.content())
    pg.click("#btn-trocar"); pg.fill("#token-novo", "TOKPRIMOS"); pg.click("#btn-salvar-token")
    expect(pg.locator("#estado")).to_have_text("Conectado", timeout=15000)
    check("trocar token pela ficha", "META_ACCESS_TOKEN=TOKPRIMOS" in (tmp / "clientes/cliente-ruim.env").read_text())
    pg.click("#btn-remover"); expect(pg.locator("#remover")).to_contain_text("apagar o token deste Mac"); pg.click("#btn-remover-sim")
    expect(pg.locator(".bilhete")).to_have_count(2)
    check("remover com confirmação na própria tela", not (tmp / "clientes/cliente-ruim.env").exists())

    pg.click(".bilhete[data-id=primos-premiacoes]"); expect(pg.locator("#miolo h1")).to_have_text("Primos Premiações")
    pg.click("[data-dias='14']")
    check("atalho 14 dias preenche as datas", pg.input_value("#desde") == (ontem - timedelta(days=13)).isoformat() and pg.input_value("#ate") == ontem.isoformat(), pg.input_value("#desde") + " a " + pg.input_value("#ate"))
    pg.click("#btn-exportar"); expect(pg.locator("#relatorios .aviso.ok")).to_contain_text("Relatório exportado", timeout=120000)
    arqs = list((tmp / "relatorios/primos-premiacoes").glob("*.xlsx"))
    check("exportar: arquivo na pasta do cliente e na tabela", len(arqs) == 1 and pg.locator("#relatorios tbody tr").count() == 1, arqs[0].name if arqs else "")
    pg.locator("#relatorios details summary").click()
    check("exportar: conferência de somas visível", "gasto A +0.00%" in pg.locator("#relatorios pre").inner_text())
    pg.locator("#relatorios").scroll_into_view_if_needed(); pg.screenshot(path=str(shots / "3-relatorio.png"))
    st, corpo = http("POST", "/api/abrir", {"cliente": "primos-premiacoes", "arquivo": "../../clientes/dona-co.env"}, **{"X-Painel-Chave": CHAVE})
    check("abrir arquivo fora da pasta do cliente: bloqueado", st == 404, f"HTTP {st}")
    pg.click("[data-mostrar]"); pg.click("#btn-pasta"); pg.wait_for_timeout(300)

    pg.click("#btn-copiar"); expect(pg.locator("#btn-copiar")).to_have_text("Instrução copiada")
    copiado = pg.evaluate("navigator.clipboard.readText()")
    check("copiar instrução do projeto", 'cliente="primos-premiacoes"' in copiado and copiado.startswith("Neste projeto"), copiado[:70])

    pg.fill("#evento", "offsite_conversion.custom.123"); pg.click("#btn-evento"); expect(pg.locator("#msg-conexao")).to_contain_text("Evento de compra salvo")
    check("evento de compra personalizado gravado", "META_PURCHASE_ACTION=offsite_conversion.custom.123" in (tmp / "clientes/primos-premiacoes.env").read_text())
    pg.fill("#evento", "auto"); pg.click("#btn-evento"); expect(pg.locator("#msg-conexao")).to_contain_text("salvo")

    expect(pg.locator("#claude")).to_contain_text("ainda não ligado"); pg.click("#btn-claude"); expect(pg.locator("#claude")).to_contain_text("Claude Desktop ligado")
    cfg = json.loads((tmp / "Claude/claude_desktop_config.json").read_text())
    check("ligar ao Claude: servidor registrado", cfg["mcpServers"]["meta-ads"]["args"][0].endswith("server.py"))

    pg.reload(); expect(pg.locator(".bilhete .ponto.ok")).to_have_count(2, timeout=20000)
    pg.evaluate("document.querySelector('.ficha').scrollTo(0, 0)"); pg.wait_for_timeout(400); pg.screenshot(path=str(shots / "2-ficha.png"))
    check("ao reabrir: testa todos e acende os dois bilhetes", True)

    st, corpo = http("GET", "/api/estado", **{"X-Painel-Chave": CHAVE})
    check("API de estado nunca devolve token", st == 200 and "TOK" not in corpo and "META_ACCESS_TOKEN" not in corpo)
    check("HTML da janela sem token", "TOKPRIMOS" not in pg.content() and "TOKDONA" not in pg.content())

    pm = ctx.new_page(); pm.set_viewport_size({"width": 390, "height": 800}); pm.goto(BASEURL + "/#k=" + CHAVE)
    expect(pm.locator(".bilhete")).to_have_count(2); pm.wait_for_timeout(1500)
    check("janela estreita: sem rolagem horizontal", pm.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), str(pm.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")))
    pm.screenshot(path=str(shots / "4-estreita.png"))
    erros = [e for e in erros if "Failed to load resource" not in e]; check("sem erros de JavaScript no console", not erros, "; ".join(erros)[:300])
    b.close()

import mock_graph  # só para o caminho; o processo do painel tem a própria cópia
srv.terminate()
print(f"\n{sum(ok)}/{len(ok)} verificações passaram"); sys.exit(0 if all(ok) else 1)
