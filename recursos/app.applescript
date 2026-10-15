-- Impera Meta MCP: app do Mac (applet que fica aberto enquanto a janela existe).
-- Gerado pelo painel.py com osacompile; __PYTHON__ e __PAINEL__ são trocados pelos caminhos reais.
-- Dock: o ponto aparece enquanto o app está aberto. Clicar no ícone com o app aberto só traz a
-- janela para a frente. Fechar a janela fecha o app; o painel continua em segundo plano só se
-- essa opção estiver ligada no próprio app.

use AppleScript version "2.4"
use framework "Foundation"
use framework "AppKit"
use framework "WebKit"
use scripting additions

property janela : missing value
property visao : missing value

on painel(argumentos)
	return do shell script (quoted form of "__PYTHON__") & " " & (quoted form of "__PAINEL__") & " " & argumentos
end painel

on run
	my mostrar()
end run

on reopen
	my mostrar()
end reopen

on mostrar()
	set endereco to ""
	try
		set endereco to my painel("--url")
	on error mensagem
		display alert "Impera Meta MCP" message ("Não consegui iniciar o painel." & return & return & mensagem) as critical
		tell me to quit
		return
	end try
	try
		if janela is missing value then my criarJanela(endereco)
		if (janela's isMiniaturized()) as boolean then janela's deminiaturize:(missing value)
		janela's makeKeyAndOrderFront:(missing value)
		current application's NSApp's activateIgnoringOtherApps:true
	on error mensagem
		-- Sem janela própria neste Mac: abre o painel no navegador e fecha o app.
		try
			my painel("--abrir-navegador --motivo " & quoted form of mensagem & " > /dev/null 2>&1 &")
		end try
		tell me to quit
	end try
end mostrar

on criarJanela(endereco)
	set ca to current application
	set quadro to {{0, 0}, {1180, 840}}
	set novaJanela to ca's NSWindow's alloc()'s initWithContentRect:quadro styleMask:15 backing:2 defer:false
	novaJanela's setReleasedWhenClosed:false
	novaJanela's setTitle:"Impera Meta MCP"
	novaJanela's setMinSize:{720, 520}
	novaJanela's |center|()
	novaJanela's setFrameAutosaveName:"ImperaMetaMCPJanela"
	set configuracao to ca's WKWebViewConfiguration's alloc()'s init()
	set novaVisao to ca's WKWebView's alloc()'s initWithFrame:quadro configuration:configuracao
	novaVisao's setAutoresizingMask:18
	novaJanela's setContentView:novaVisao
	novaVisao's loadRequest:(ca's NSURLRequest's requestWithURL:(ca's NSURL's URLWithString:endereco))
	novaJanela's setDelegate:me
	set janela to novaJanela
	set visao to novaVisao
	try
		my criarMenus()
	end try
end criarJanela

-- Sem um menu Editar, Cmd+C e Cmd+V não chegam à janela (e o token precisa ser colado).
on criarMenus()
	set principal to current application's NSApp's mainMenu()
	if principal is missing value then return
	if (principal's itemWithTitle:"Editar") is missing value and (principal's itemWithTitle:"Edit") is missing value then
		my adicionarMenu(principal, "Editar", {{"Desfazer", "undo:", "z"}, {"Refazer", "redo:", "Z"}, {"Recortar", "cut:", "x"}, {"Copiar", "copy:", "c"}, {"Colar", "paste:", "v"}, {"Selecionar tudo", "selectAll:", "a"}})
	end if
	if (principal's itemWithTitle:"Janela") is missing value and (principal's itemWithTitle:"Window") is missing value then
		my adicionarMenu(principal, "Janela", {{"Minimizar", "performMiniaturize:", "m"}, {"Fechar", "performClose:", "w"}, {"Recarregar", "reload:", "r"}})
	end if
end criarMenus

on adicionarMenu(principal, titulo, itens)
	set ca to current application
	set submenu to ca's NSMenu's alloc()'s initWithTitle:titulo
	repeat with i from 1 to count of itens
		set par to item i of itens
		(submenu's addItemWithTitle:(item 1 of par) action:(item 2 of par) keyEquivalent:(item 3 of par))
	end repeat
	set itemPrincipal to ca's NSMenuItem's alloc()'s initWithTitle:titulo action:(missing value) keyEquivalent:""
	itemPrincipal's setSubmenu:submenu
	principal's addItem:itemPrincipal
end adicionarMenu

-- Fechar a janela fecha o app (o ponto some do Dock).
on windowWillClose:aviso
	current application's NSApp's performSelector:"terminate:" withObject:(missing value) afterDelay:0
end windowWillClose:

on quit
	try
		-- Com o segundo plano desligado, encerra o painel; ligado, ele continua ativo.
		my painel("--ao-fechar-app > /dev/null 2>&1 &")
	end try
	continue quit
end quit
