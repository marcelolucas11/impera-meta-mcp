import json, subprocess, sys
py, wrap, proj = sys.argv[1:4]
p = subprocess.Popen([py, wrap, proj], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
def send(m): p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
def recv():
    while True:
        line = p.stdout.readline()
        if not line: raise SystemExit("servidor fechou: " + p.stderr.read()[-3000:])
        m = json.loads(line)
        if "id" in m: return m
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}); recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}); tools = recv()["result"]["tools"]
print("ferramentas:", [t["name"] for t in tools])
assert all("cliente" in t["inputSchema"].get("properties", {}) for t in tools if t["name"] != "listar_clientes")
escrita = {t["name"]: t.get("annotations") or {} for t in tools if t["name"] in ("alterar_status", "alterar_orcamento", "renomear", "duplicar", "criar_campanha", "criar_conjunto", "criar_anuncio", "criar_publico_lista", "criar_publico_site", "criar_publico_engajamento", "criar_publico_semelhante", "alterar_publico", "atualizar_publico_lista", "atualizar_conjunto", "atualizar_campanha", "criar_criativo", "enviar_video", "criar_conversao_personalizada", "criar_formulario_leads", "criar_regra_automatica", "enviar_eventos_conversao", "criar_teste_ab", "criar_catalogo", "criar_feed_produtos", "criar_regra_feed", "criar_conjunto_produtos", "criar_produtos", "atualizar_produtos", "criar_criativo_avancado", "criar_pixel", "moderar_comentario", "responder_comentario", "dar_acesso_ativo")}
assert len(escrita) == 33 and all(a.get("readOnlyHint") is False for a in escrita.values()), escrita
print("ferramentas de alteração marcadas como não somente leitura:", sorted(escrita))
# 6 chamadas disparadas de uma vez, alternando clientes
for i in range(6):
    send({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": "consultar_insights", "arguments": {"cliente": "primos" if i % 2 == 0 else "donaco", "desde": "2026-09-01", "ate": "2026-09-02"}}})
ordem, ok = [], True
for _ in range(6):
    r = recv(); i = r["id"] - 10; esperado = "primos" if i % 2 == 0 else "donaco"; txt = r["result"]["content"][0]["text"]
    certo = f"Cliente: {esperado}" in txt and f"campanha-do-{esperado}" in txt and ("111.00" if esperado == "primos" else "333.00") in txt and "ERRO" not in txt
    ordem.append(f"{i}:{esperado}:{'ok' if certo else 'ERRADO'}"); ok &= certo
    if not certo: print(txt[:300])
print("respostas na ordem de chegada:", " ".join(ordem))
send({"jsonrpc": "2.0", "id": 30, "method": "tools/call", "params": {"name": "consultar_insights", "arguments": {}}})
print("sem cliente ->", recv()["result"]["content"][0]["text"][:95])
p.terminate(); assert ok and len(tools) == 78, len(tools); print("OK")
