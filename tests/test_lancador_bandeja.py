"""Ciclo de vida do modo bandeja (`executar_com_bandeja`).

O executável instalado não tem janela de console — fechar algo sem querer não
pode mais derrubar o servidor (era exatamente esse o problema que motivou a
mudança). Só "Sair" no menu do ícone da bandeja encerra de verdade.

`pystray` e `pillow` só existem em `requirements-empacotamento.txt` (são
importados de forma preguiçosa dentro de `executar_com_bandeja`, só no
caminho do executável) — por isso os testes aqui usam dublês registrados em
`sys.modules`, e passam em qualquer ambiente de desenvolvimento, sem precisar
instalar nada a mais.
"""

from __future__ import annotations

import sys
import threading
import time
import types
import urllib.request

import pytest


class _MenuItemFalso:
    def __init__(self, rotulo, acao, default=False):
        self.rotulo = rotulo
        self.acao = acao
        self.default = default


class _MenuFalso:
    def __init__(self, *itens):
        self.itens = itens

    def _clicar(self, rotulo, icone):
        for item in self.itens:
            if item.rotulo == rotulo:
                item.acao(icone, item)
                return
        raise AssertionError(f"menu sem item {rotulo!r}")


class _IconeFalso:
    """Substitui `pystray.Icon`: sem backend gráfico, mas com o mesmo contrato
    (`run()` bloqueia até `stop()`), suficiente para testar a lógica do
    lançador sem depender de bandeja de verdade.

    Guarda cada instância criada em `_instancias`, para o teste achar o menu
    real que `executar_com_bandeja` montou e simular um clique nele."""

    _instancias: list["_IconeFalso"] = []

    def __init__(self, nome, imagem, titulo, menu):
        self.nome, self.imagem, self.titulo, self.menu = nome, imagem, titulo, menu
        self._evento_parar = threading.Event()
        _IconeFalso._instancias.append(self)

    def run(self):
        # Timeout generoso: é só uma rede de segurança contra o teste travar
        # para sempre se esquecer de chamar stop(); os testes sempre chamam.
        self._evento_parar.wait(timeout=30)

    def stop(self):
        self._evento_parar.set()


@pytest.fixture
def pystray_falso(monkeypatch):
    _IconeFalso._instancias.clear()
    modulo = types.ModuleType("pystray")
    modulo.Icon = _IconeFalso
    modulo.MenuItem = _MenuItemFalso
    modulo.Menu = _MenuFalso
    monkeypatch.setitem(sys.modules, "pystray", modulo)
    return modulo


@pytest.fixture
def pil_falso(monkeypatch):
    """`_icone_da_bandeja` usa `Image.open` quando `empacotamento/icone.ico`
    existe (é o caso normal, já que o ícone está versionado no repositório) e
    cai para `Image.new` + `ImageDraw` só se o arquivo não estiver lá."""
    modulo_image = types.ModuleType("PIL.Image")
    modulo_image.new = lambda modo, tamanho, cor: object()
    modulo_image.open = lambda caminho: object()

    class _DesenhoFalso:
        def rounded_rectangle(self, *a, **k):
            pass

    modulo_draw = types.ModuleType("PIL.ImageDraw")
    modulo_draw.Draw = lambda imagem: _DesenhoFalso()

    modulo_pil = types.ModuleType("PIL")
    monkeypatch.setitem(sys.modules, "PIL", modulo_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", modulo_image)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", modulo_draw)


@pytest.fixture
def sem_navegador_de_verdade(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: urls.append(url))
    return urls


@pytest.fixture(autouse=True)
def dados_isolados(tmp_path, monkeypatch):
    """`executar_com_bandeja` agora também dispara a verificação periódica de
    recorrências, que lê a configuração do app — sem isto, os testes tocariam
    a pasta de dados real de quem estiver rodando a suíte."""
    monkeypatch.setenv("EMISSOR_NFSE_DIR", str(tmp_path / "dados"))


def test_sair_do_menu_encerra_o_servidor_de_verdade(
    pystray_falso, pil_falso, sem_navegador_de_verdade
):
    """Prova o ciclo completo com um servidor real na porta: ele responde
    enquanto ativo, "Sair" encerra de forma graciosa, e depois ele para de
    responder — sem precisar de bandeja gráfica nenhuma para isso ser verdade."""
    from app import lancador

    porta = 8853
    resultado: dict[str, bool] = {}

    def rodar():
        lancador.executar_com_bandeja(porta=porta)
        resultado["retornou"] = True

    thread = threading.Thread(target=rodar, daemon=True)
    thread.start()

    # Espera a inicialização (a abertura automática do navegador tem 1s de atraso).
    for _ in range(50):
        try:
            resposta = urllib.request.urlopen(f"http://127.0.0.1:{porta}/", timeout=1)
            assert resposta.status == 200
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail("o servidor não respondeu a tempo")

    # A abertura automática do navegador tem 1s de atraso proposital (dar
    # tempo do servidor subir) — espera passar disso antes de conferir.
    time.sleep(1.2)
    assert sem_navegador_de_verdade, "deveria ter aberto o navegador sozinho"
    assert sem_navegador_de_verdade[0].startswith(f"http://127.0.0.1:{porta}/?t=")

    # Ninguém pediu para sair ainda — o servidor deveria continuar de pé.
    assert thread.is_alive(), "o servidor não deveria parar sozinho"

    # Acha o ícone real criado por executar_com_bandeja através do dublê e
    # simula o clique em "Sair" — é o único jeito de derrubar o servidor agora.
    icones_criados = _IconeFalso._instancias
    assert len(icones_criados) == 1
    icones_criados[0].menu._clicar("Sair", icones_criados[0])

    thread.join(timeout=5)
    assert resultado.get("retornou"), "executar_com_bandeja não retornou após Sair"

    # O uvicorn fecha o socket de escuta como parte do próprio desligamento
    # gracioso — pode levar uma fração de segundo depois de `run()` retornar.
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{porta}/", timeout=1)
            time.sleep(0.1)
        except OSError:
            break
    else:
        pytest.fail("o servidor continuou respondendo depois do Sair")


def test_clicar_no_icone_reabre_o_painel(pystray_falso, pil_falso, sem_navegador_de_verdade):
    from app import lancador

    porta = 8854
    thread = threading.Thread(
        target=lancador.executar_com_bandeja, kwargs={"porta": porta}, daemon=True
    )
    thread.start()

    for _ in range(50):
        if _IconeFalso._instancias:
            break
        time.sleep(0.1)
    icone = _IconeFalso._instancias[-1]

    icone.menu._clicar("Abrir painel", icone)
    assert len(sem_navegador_de_verdade) >= 1
    assert all(u.startswith(f"http://127.0.0.1:{porta}/?t=") for u in sem_navegador_de_verdade)

    icone.menu._clicar("Sair", icone)
    thread.join(timeout=5)
