"""Confere uma instalação de verdade no Windows (roda no GitHub Actions, no runner windows, depois do instalar-windows.ps1).

  python testes/test_windows.py

Confere: pasta e Python do app, atalhos do Menu Iniciar e da Área de Trabalho, registro no Claude Desktop,
o servidor MCP respondendo pelo stdio exatamente como o Claude o sobe, o painel no ar (e encerrando) e a trava
de arquivo da fila de escritas (msvcrt). Nenhum token real, nenhuma chamada à Meta.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ok = []


def check(nome, cond, detalhe=""):
    ok.append(bool(cond)); print(("PASS " if cond else "FAIL ") + nome + (f"  [{str(detalhe)[:300]}]" if detalhe else ""), flush=True)


assert sys.platform.startswith("win"), "este teste é só para Windows"
INST = Path(os.environ["USERPROFILE"]) / "meta-ads-mcp"
PY = INST / ".venv" / "Scripts" / "python.exe"
check("programa instalado na pasta do usuário", (INST / "VERSAO").is_file() and (INST / "server.py").is_file(), INST)
check("Python próprio do app (.venv)", PY.is_file(), PY)
check("pythonw.exe para abrir sem janela preta", (INST / ".venv" / "Scripts" / "pythonw.exe").is_file())
check("arquivos de desenvolvimento não vão para a instalação", not (INST / "testes").exists() and not (INST / ".github").exists())

menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Impera Meta MCP.lnk"
check("atalho no Menu Iniciar", menu.is_file(), menu)
desk = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                      capture_output=True, text=True).stdout.strip()
check("atalho na Área de Trabalho", (Path(desk) / "Impera Meta MCP.lnk").is_file(), desk)
alvo = subprocess.run(["powershell", "-NoProfile", "-Command",
                       f"$l=(New-Object -ComObject WScript.Shell).CreateShortcut('{menu}'); $l.TargetPath + '|' + $l.Arguments + '|' + $l.IconLocation"],
                      capture_output=True, text=True).stdout.strip()
check("atalho abre o painel com o Python do app e a logo", "pythonw.exe" in alvo and "painel.py" in alvo and "icone.ico" in alvo, alvo)

cfg_path = Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
entrada = {}
try:
    entrada = json.loads(cfg_path.read_text(encoding="utf-8"))["mcpServers"]["meta-ads"]
except (OSError, ValueError, KeyError) as e:
    check("registro no Claude Desktop", False, e)
check("servidor meta-ads registrado no Claude Desktop", entrada.get("command", "").lower().endswith("python.exe")
      and entrada.get("args", [])[-1:] == [str(INST / "server.py")] and "-X" in entrada.get("args", []), entrada)

# O servidor MCP, subido exatamente como o Claude Desktop sobe.
if entrada:
    p = subprocess.Popen([entrada["command"], *entrada["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=str(Path.home()))

    def send(m):
        p.stdin.write((json.dumps(m) + "\n").encode("utf-8")); p.stdin.flush()

    def recv():
        while True:
            linha = p.stdout.readline()
            if not linha:
                raise SystemExit("servidor fechou: " + p.stderr.read().decode("utf-8", "replace")[-3000:])
            m = json.loads(linha.decode("utf-8"))
            if "id" in m:
                return m

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                         "clientInfo": {"name": "teste-windows", "version": "0"}}})
    ini = recv()
    check("servidor MCP responde ao initialize", "result" in ini, ini.get("result", {}).get("serverInfo"))
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = recv().get("result", {}).get("tools", [])
    check("servidor MCP lista as ferramentas", len(tools) >= 70, len(tools))
    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "listar_clientes", "arguments": {}}})
    txt = recv()["result"]["content"][0]["text"]
    check("listar_clientes funciona (acentos certos)", "Nenhum cliente configurado" in txt and "�" not in txt, txt[:120])
    p.terminate()

# Painel: sobe, responde e encerra.
env = dict(os.environ, PAINEL_SEM_JANELA="1")
r = subprocess.run([str(PY), "-X", "utf8", str(INST / "painel.py"), "--sem-janela"], capture_output=True, text=True,
                   encoding="utf-8", timeout=90, env=env, cwd=str(INST))
check("painel sobe", "Painel no ar" in r.stdout, (r.stdout + r.stderr)[-300:])
est = {}
try:
    est = json.loads((INST / ".painel.json").read_text())
    req = urllib.request.Request(f"http://127.0.0.1:{est['porta']}/api/estado", headers={"X-Painel-Chave": est["chave"]})
    with urllib.request.urlopen(req, timeout=10) as resp:
        dados = json.loads(resp.read().decode("utf-8"))
    check("painel responde e se identifica como Windows", dados.get("sistema") == "windows"
          and dados.get("versao") == (INST / "VERSAO").read_text().strip(), {k: dados.get(k) for k in ("sistema", "versao")})
    check("painel enxerga o Claude Desktop ligado", dados.get("claude", {}).get("registrado") is True, dados.get("claude"))
    check("segundo plano fica desligado no Windows", dados.get("segundo_plano_disponivel") is False)
    req = urllib.request.Request(f"http://127.0.0.1:{est['porta']}/", headers={})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    check("janela do painel carrega", "Impera" in html)
except Exception as e:  # noqa: BLE001
    check("painel responde", False, repr(e))
r = subprocess.run([str(PY), "-X", "utf8", str(INST / "painel.py"), "--encerrar"], capture_output=True, text=True,
                   encoding="utf-8", timeout=60, cwd=str(INST))
time.sleep(1)
check("painel encerra", "Painel encerrado" in r.stdout and not (INST / ".painel.json").exists(), r.stdout.strip())

# Trava de arquivo da fila de escritas (no Windows, msvcrt em vez de fcntl).
r = subprocess.run([str(PY), "-X", "utf8", "-c", (
    "import sys, tempfile; sys.path.insert(0, sys.argv[1]); import protecao as p; d = tempfile.mkdtemp();"
    "a = p._reservar_vaga(d, 'k'); b = p._reservar_vaga(d, 'k');"
    "import json, pathlib; j = json.loads((pathlib.Path(d) / p.ARQUIVO_RITMO).read_text());"
    "print('ok' if b >= a + p.INTERVALO_ESCRITA - 0.01 and 'k' in j else 'ruim', a, b)"), str(INST)],
    capture_output=True, text=True, timeout=60)
check("fila de escritas com trava de arquivo funciona no Windows", r.stdout.startswith("ok"), (r.stdout + r.stderr)[-300:])

print(f"\n{sum(ok)}/{len(ok)} verificações passaram")
sys.exit(0 if all(ok) else 1)
