"""Testes da recorrência: quem vence, e o repositório de pendências."""

from __future__ import annotations

from datetime import date

import pytest

from nfse.clientes import BancoLocal, Cliente, ErroCadastro, RepositorioClientes, RepositorioRecorrencias
from nfse.recorrencia import clientes_vencidos, competencia_de


def _cliente(**sobrepor) -> Cliente:
    base = dict(
        documento="11.222.333/0001-81",
        razao_social="Cliente Alfa Tecnologia LTDA",
        email="financeiro@alfa.com.br",
    )
    base.update(sobrepor)
    return Cliente(**base)


# ---------------------------------------------------------------------------
# Cadastro: validação dos campos de recorrência
# ---------------------------------------------------------------------------
def test_dia_fora_do_intervalo_e_recusado():
    with pytest.raises(ErroCadastro, match="entre 1 e 28"):
        _cliente(dia_emissao_recorrente=29, descricao_recorrente="Consultoria.").validar()
    with pytest.raises(ErroCadastro, match="entre 1 e 28"):
        _cliente(dia_emissao_recorrente=0 - 1, descricao_recorrente="Consultoria.").validar()


def test_dia_recorrente_exige_descricao():
    with pytest.raises(ErroCadastro, match="Descreva o serviço recorrente"):
        _cliente(dia_emissao_recorrente=5).validar()


def test_valor_recorrente_normaliza_formato_brasileiro():
    cliente = _cliente(dia_emissao_recorrente=5, descricao_recorrente="Consultoria.",
                       valor_recorrente="1.900,50")
    cliente.validar()
    assert cliente.valor_recorrente == "1900.50"


def test_valor_recorrente_invalido_e_recusado():
    with pytest.raises(ErroCadastro, match="Valor mensal recorrente inválido"):
        _cliente(valor_recorrente="abc").validar()
    with pytest.raises(ErroCadastro, match="Valor mensal recorrente inválido"):
        _cliente(valor_recorrente="0").validar()


def test_cliente_sem_dia_nao_e_recorrente():
    cliente = _cliente()
    cliente.validar()
    assert cliente.recorrente is False


def test_cliente_com_dia_e_recorrente():
    cliente = _cliente(dia_emissao_recorrente=10, descricao_recorrente="Consultoria.")
    cliente.validar()
    assert cliente.recorrente is True


# ---------------------------------------------------------------------------
# clientes_vencidos: lógica pura
# ---------------------------------------------------------------------------
def test_vence_no_dia_e_depois_dele():
    cliente = _cliente(dia_emissao_recorrente=10, descricao_recorrente="Consultoria.")
    assert clientes_vencidos([cliente], date(2026, 9, 10)) == [cliente]
    assert clientes_vencidos([cliente], date(2026, 9, 15)) == [cliente]


def test_nao_vence_antes_do_dia():
    cliente = _cliente(dia_emissao_recorrente=10, descricao_recorrente="Consultoria.")
    assert clientes_vencidos([cliente], date(2026, 9, 9)) == []


def test_cliente_inativo_nunca_vence():
    cliente = _cliente(dia_emissao_recorrente=1, descricao_recorrente="Consultoria.", ativo=False)
    assert clientes_vencidos([cliente], date(2026, 9, 28)) == []


def test_cliente_nao_recorrente_nunca_vence():
    cliente = _cliente()
    assert clientes_vencidos([cliente], date(2026, 9, 28)) == []


def test_competencia_de_formata_ano_mes():
    assert competencia_de(date(2026, 9, 14)) == "2026-09"


# ---------------------------------------------------------------------------
# RepositorioRecorrencias
# ---------------------------------------------------------------------------
@pytest.fixture
def repo_rec(tmp_path) -> RepositorioRecorrencias:
    return RepositorioRecorrencias(BancoLocal(tmp_path / "dados.db"))


def test_criar_pendencia_e_idempotente(repo_rec):
    _, criada1 = repo_rec.criar_pendencia("11222333000181", "2026-09", "1900.00", "Consultoria.",
                                          "aguardando_aprovacao")
    _, criada2 = repo_rec.criar_pendencia("11222333000181", "2026-09", "9999.00", "Outra coisa",
                                          "aguardando_valor")
    assert criada1 is True
    assert criada2 is False, "já existia uma pendência para este cliente nesta competência"

    pendentes = repo_rec.listar_pendentes()
    assert len(pendentes) == 1
    assert pendentes[0].valor == "1900.00", "a segunda tentativa não deve sobrescrever a primeira"


def test_pendencia_de_competencia_diferente_nao_colide(repo_rec):
    repo_rec.criar_pendencia("11222333000181", "2026-08", "1900.00", "Consultoria.", "aguardando_aprovacao")
    _, criada = repo_rec.criar_pendencia("11222333000181", "2026-09", "1900.00", "Consultoria.",
                                         "aguardando_aprovacao")
    assert criada is True
    assert len(repo_rec.listar_pendentes()) == 2


def test_marcar_emitida_sai_da_lista_de_pendentes(repo_rec):
    recorrencia, _ = repo_rec.criar_pendencia("11222333000181", "2026-09", "1900.00", "Consultoria.",
                                              "aguardando_aprovacao")
    repo_rec.marcar_emitida(recorrencia.id, "CHAVE123")

    assert repo_rec.listar_pendentes() == []
    resolvida = repo_rec.buscar(recorrencia.id)
    assert resolvida.estado == "emitida"
    assert resolvida.chave_acesso == "CHAVE123"


def test_marcar_pulada_sai_da_lista_de_pendentes(repo_rec):
    recorrencia, _ = repo_rec.criar_pendencia("11222333000181", "2026-09", "", "", "aguardando_valor")
    repo_rec.marcar_pulada(recorrencia.id)

    assert repo_rec.listar_pendentes() == []
    assert repo_rec.buscar(recorrencia.id).estado == "pulada"


def test_definir_valor_atualiza_uma_pendencia_aguardando_valor(repo_rec):
    recorrencia, _ = repo_rec.criar_pendencia("11222333000181", "2026-09", "", "", "aguardando_valor")
    repo_rec.definir_valor(recorrencia.id, "1234.56")
    assert repo_rec.buscar(recorrencia.id).valor == "1234.56"


def test_migracao_adiciona_colunas_a_banco_antigo(tmp_path):
    """Simula um cadastro salvo antes dos campos de recorrência existirem."""
    import sqlite3

    caminho = tmp_path / "antigo.db"
    with sqlite3.connect(caminho) as conexao:
        conexao.execute("""
            CREATE TABLE clientes (
                documento TEXT PRIMARY KEY, razao_social TEXT NOT NULL, email TEXT DEFAULT '',
                logradouro TEXT DEFAULT '', numero TEXT DEFAULT '', complemento TEXT DEFAULT '',
                bairro TEXT DEFAULT '', cod_municipio TEXT DEFAULT '', uf TEXT DEFAULT '',
                cep TEXT DEFAULT '', telefone TEXT DEFAULT '', ativo INTEGER NOT NULL DEFAULT 1,
                receber_por_email INTEGER NOT NULL DEFAULT 1, observacao TEXT DEFAULT '',
                criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL
            )
        """)
        conexao.execute(
            "INSERT INTO clientes (documento, razao_social, criado_em, atualizado_em) "
            "VALUES ('11222333000181', 'Cliente Antigo', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )

    repo = RepositorioClientes(BancoLocal(caminho))  # dispara a migração
    cliente = repo.buscar("11222333000181")
    assert cliente.dia_emissao_recorrente == 0
    assert cliente.recorrente is False

    salvo = repo.salvar(_cliente(dia_emissao_recorrente=5, descricao_recorrente="Consultoria."))
    assert salvo.dia_emissao_recorrente == 5
