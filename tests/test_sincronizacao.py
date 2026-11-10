"""Dois "computadores" (dois bancos) trocando dados pela mesma pasta compartilhada."""

from __future__ import annotations

from datetime import datetime

import pytest

from nfse import sincronizacao
from nfse.clientes import (
    BancoLocal, Cliente, RepositorioClientes, RepositorioEmissoes, RepositorioRecibos,
    RepositorioRecorrencias,
)


@pytest.fixture(autouse=True)
def _desliga_no_fim():
    yield
    sincronizacao.desativar()


@pytest.fixture
def ambiente(tmp_path):
    pasta = tmp_path / "Drive" / "Emissor NFS-e"
    pasta.mkdir(parents=True)
    bancos = {}
    for serie in ("1", "2"):
        (tmp_path / f"pc{serie}").mkdir()
        bancos[serie] = tmp_path / f"pc{serie}" / "dados.db"
        BancoLocal(bancos[serie])   # cria as tabelas
    return pasta, bancos


def como(ambiente, serie: str) -> BancoLocal:
    """Passa a "estar" no computador da série dada."""
    pasta, bancos = ambiente
    sincronizacao.ativar(pasta, serie, bancos[serie])
    return BancoLocal(bancos[serie])


def rodada(ambiente, serie: str) -> int:
    como(ambiente, serie)
    sincronizacao.descarregar()
    return sincronizacao.importar()


def _cliente(nome="Cliente Alfa", **extra) -> Cliente:
    return Cliente(documento="11222333000181", razao_social=nome, email="a@b.com.br", **extra)


def test_cliente_cadastrado_num_computador_aparece_no_outro(ambiente):
    RepositorioClientes(como(ambiente, "1")).salvar(_cliente())
    rodada(ambiente, "1")
    assert rodada(ambiente, "2") >= 1
    assert RepositorioClientes(como(ambiente, "2")).buscar("11222333000181").razao_social == "Cliente Alfa"


def _relogio(monkeypatch, quando: datetime) -> None:
    """Faz o cadastro "achar" que agora é `quando` (para ordenar edições)."""
    monkeypatch.setattr("nfse.clientes.datetime", type("Relogio", (), {"now": staticmethod(lambda: quando)}))


def test_edicao_mais_nova_vence_e_exclusao_propaga(ambiente, monkeypatch):
    RepositorioClientes(como(ambiente, "1")).salvar(_cliente("Nome antigo"))
    rodada(ambiente, "1")
    rodada(ambiente, "2")

    # Edição no computador 2, depois.
    repo2 = RepositorioClientes(como(ambiente, "2"))
    cliente = repo2.buscar("11222333000181")
    cliente.razao_social = "Nome novo"
    _relogio(monkeypatch, datetime(2099, 1, 1))
    repo2.salvar(cliente)
    rodada(ambiente, "2")
    rodada(ambiente, "1")
    assert RepositorioClientes(como(ambiente, "1")).buscar("11222333000181").razao_social == "Nome novo"

    # Exclusão no computador 1, mais nova ainda, apaga no 2.
    _relogio(monkeypatch, datetime(2099, 1, 2))
    RepositorioClientes(como(ambiente, "1")).excluir("11222333000181")
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    assert RepositorioClientes(como(ambiente, "2")).buscar("11222333000181") is None


def test_nota_emitida_aparece_no_historico_do_outro(ambiente):
    RepositorioEmissoes(como(ambiente, "1")).registrar({
        "chave_acesso": "CHAVE-A", "documento_tomador": "11222333000181", "tomador": "Cliente Alfa",
        "valor_servico": "4500.00", "n_nfse": "84", "competencia": "2026-09",
        "emitida_em": "2026-10-01T10:00:00", "ambiente": "producao",
        "arquivos": {"xml_nfse": "C:/so/no/pc1.xml"},
    })
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    nota = RepositorioEmissoes(como(ambiente, "2")).listar(documento="11222333000181")[0]
    assert nota["n_nfse"] == "84" and nota["tomador"] == "Cliente Alfa"
    assert nota["arquivos"] == {}      # caminhos do outro computador não valem aqui


def test_recibos_de_computadores_diferentes_nao_repetem_numero(ambiente):
    r1 = RepositorioRecibos(como(ambiente, "1"))
    n1 = r1.criar("11222333000181", "Alfa", "", "100.00", "x", "2026-10-07", serie="1")
    r2 = RepositorioRecibos(como(ambiente, "2"))
    n2 = r2.criar("11222333000181", "Alfa", "", "200.00", "y", "2026-10-08", serie="2")
    assert r1.buscar(n1)["rotulo"] == "1-0001"
    assert r2.buscar(n2)["rotulo"] == "2-0001"

    for serie in ("1", "2", "1"):
        rodada(ambiente, serie)
    rotulos = sorted(r["rotulo"] for r in RepositorioRecibos(como(ambiente, "1")).listar())
    assert rotulos == ["1-0001", "2-0001"]
    # O próximo do computador 1 continua a sequência dele.
    seguinte = RepositorioRecibos(como(ambiente, "1")).criar("1", "A", "", "1", "z", "2026-10-09", serie="1")
    assert RepositorioRecibos(como(ambiente, "1")).buscar(seguinte)["rotulo"] == "1-0002"


def test_linha_incompleta_espera_o_drive_terminar(ambiente):
    pasta, _ = ambiente
    RepositorioClientes(como(ambiente, "1")).salvar(_cliente())
    rodada(ambiente, "1")
    arquivo = pasta / "computadores" / "serie-1" / "eventos.jsonl"
    completo = arquivo.read_bytes()
    arquivo.write_bytes(completo[:-10])          # Drive ainda copiando
    assert rodada(ambiente, "2") == 0
    arquivo.write_bytes(completo)
    assert rodada(ambiente, "2") == 1
    assert rodada(ambiente, "2") == 0           # não reaplica


def test_arquivo_truncado_rele_sem_duplicar(ambiente):
    pasta, _ = ambiente
    RepositorioClientes(como(ambiente, "1")).salvar(_cliente())
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    arquivo = pasta / "computadores" / "serie-1" / "eventos.jsonl"
    arquivo.write_bytes(arquivo.read_bytes()[:5])   # recriado do zero
    rodada(ambiente, "2")
    assert len(RepositorioClientes(como(ambiente, "2")).listar()) == 1


def test_sem_drive_a_alteracao_espera_na_fila(ambiente, tmp_path):
    pasta, bancos = ambiente
    sumida = tmp_path / "Drive-desconectado"
    sincronizacao.ativar(sumida, "1", bancos["1"])
    RepositorioClientes(BancoLocal(bancos["1"])).salvar(_cliente())
    assert sincronizacao.descarregar() == 0
    assert sincronizacao.pendentes_na_fila() == 1

    sincronizacao.ativar(pasta, "1", bancos["1"])   # Drive voltou
    assert sincronizacao.descarregar() == 1
    assert sincronizacao.pendentes_na_fila() == 0


def test_desligado_nao_publica_nada(tmp_path):
    banco = tmp_path / "dados.db"
    RepositorioClientes(BancoLocal(banco)).salvar(_cliente())
    assert not (tmp_path / "sync-saida.jsonl").exists()


def test_nota_mensal_emitida_vence_pendencia(ambiente):
    rec1 = RepositorioRecorrencias(como(ambiente, "1"))
    pendencia, _ = rec1.criar_pendencia("11222333000181", "2026-10", "100.00", "x", "aguardando_aprovacao")
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    rec1 = RepositorioRecorrencias(como(ambiente, "1"))
    rec1.marcar_emitida(pendencia.id, "CHAVE-M")
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    copia = RepositorioRecorrencias(como(ambiente, "2")).listar_recentes()[0]
    assert copia.estado == "emitida" and copia.chave_acesso == "CHAVE-M"


def test_primeira_ativacao_publica_o_que_ja_existia(ambiente):
    _, bancos = ambiente
    RepositorioClientes(BancoLocal(bancos["1"])).salvar(_cliente())   # antes de compartilhar
    como(ambiente, "1")
    assert sincronizacao.publicar_tudo() is True
    assert sincronizacao.publicar_tudo() is False                      # só uma vez
    rodada(ambiente, "1")
    rodada(ambiente, "2")
    assert RepositorioClientes(como(ambiente, "2")).buscar("11222333000181") is not None


def test_backup_do_banco_e_restauracao(ambiente, tmp_path):
    pasta, bancos = ambiente
    RepositorioClientes(como(ambiente, "1")).salvar(_cliente())
    RepositorioRecibos(como(ambiente, "1")).criar("11222333000181", "Alfa", "", "1.00", "x",
                                                   "2026-10-07", serie="1")
    rodada(ambiente, "1")
    assert sincronizacao.snapshot().is_file()
    assert [b["serie"] for b in sincronizacao.backups_disponiveis()] == ["1"]

    # Computador 1 formatado: banco novo, vazio, mesma série.
    novo = tmp_path / "pc1-novo" / "dados.db"
    novo.parent.mkdir()
    BancoLocal(novo)
    sincronizacao.ativar(pasta, "1", novo)
    sincronizacao.restaurar("1")
    assert RepositorioClientes(BancoLocal(novo)).buscar("11222333000181") is not None
    assert RepositorioRecibos(BancoLocal(novo)).listar()[0]["rotulo"] == "1-0001"


def test_computador_que_nao_emite_mensais_nao_cria_pendencia(ambiente, tmp_path, monkeypatch):
    from app.recorrencias import verificar_pendencias
    from nfse import armazenamento_config as ac

    monkeypatch.setenv("EMISSOR_NFSE_DIR", str(tmp_path / "app"))
    banco = ac.diretorio_dados() / "dados.db"
    RepositorioClientes(BancoLocal(banco)).salvar(_cliente(
        dia_emissao_recorrente=1, descricao_recorrente="Mensal", valor_recorrente="100,00"))

    pasta, _ = ambiente
    sincronizacao.ativar(pasta, "2", banco)
    config = ac.ConfiguracaoApp(mensais_neste_computador=False)
    assert verificar_pendencias(config) == []
    assert RepositorioRecorrencias(BancoLocal(banco)).listar_pendentes() == []

    config.mensais_neste_computador = True      # dia 1 já passou em qualquer data
    assert len(verificar_pendencias(config)) == 1
