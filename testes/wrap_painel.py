"""Sobe o painel real numa pasta temporária, com a Graph API falsa."""
import sys
from pathlib import Path
import httpx
proj, tmp, porta = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, proj); sys.path.insert(0, str(Path(__file__).parent))
import painel, configurar, server, mock_graph
server.BASE_DIR, server.ENV_FILE, server.CLIENTES_DIR = tmp, tmp / ".env", tmp / "clientes"
configurar.BASE, configurar.ENV, configurar.CLIENTES = tmp, tmp / ".env", tmp / "clientes"
configurar.caminho_config_claude = lambda: tmp / "Claude" / "claude_desktop_config.json"
painel.ESTADO = tmp / ".painel.json"; painel.ABRIR_NO_SISTEMA = False
server._TRANSPORT = httpx.MockTransport(mock_graph.handler); server._BACKOFF = (0, 0, 0)
painel.servir(porta, 600)
