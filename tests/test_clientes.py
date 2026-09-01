"""Testes do cadastro de clientes, do histórico e da ponte com a emissão."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from nfse.clientes import (
    BancoLocal,
    Cliente,
    ErroCadastro,
    RepositorioClientes,
    RepositorioEmissoes,
    completar_com_cadastro,
)
from nfse.planilha import LinhaFaturamento


@pytest.fixture
def repo(tmp_path) -> RepositorioClientes:
    return RepositorioClientes(BancoLocal(tmp_path / "dados.db"))


@pytest.fixture
def emissoes(tmp_path) -> RepositorioEmissoes:
    return RepositorioEmissoes(BancoLocal(tmp_path / "dados.db"))


def _cliente(**sobrepor) -> Cliente:
    base = dict(
        documento="11.222.333/0001-81",
        razao_social="Cliente Alfa Tecnologia LTDA",
        email="financeiro@alfa.com.br",
        logradouro="Avenida Rio Branco",
        numero="156",
        bairro="Centro",
        cod_municipio="3304557",
        uf="RJ",
        cep="20040901",
    )
    base.update(sobrepor)
    return Cliente(**base)


# ---------------------------------------------------------------------------
# Cadastro
# ---------------------------------------------------------------------------
def test_salvar_normaliza_o_documento(repo):
    salvo = repo.salvar(_cliente())
    assert salvo.documento == "11222333000181", "máscara deve ser removida"
    assert repo.buscar("11.222.333/0001-81") is not None, "busca aceita com máscara"


def test_documento_invalido_e_recusado(repo):
    with pytest.raises(ErroCadastro, match="dígito verificador"):
        repo.salvar(_cliente(documento="11222333000100"))
    with pytest.raises(ErroCadastro, match="CNPJ .* ou CPF"):
        repo.salvar(_cliente(documento="123"))


def test_marcar_para_receber_sem_email_e_recusado(repo):
    with pytest.raises(ErroCadastro, match="sem endereço de e-mail"):
        repo.salvar(_cliente(email="", receber_por_email=True))


def test_cliente_sem_email_pode_existir_se_nao_recebe(repo):
    """Cliente cujo contador busca o arquivo direto não precisa de e-mail."""
    salvo = repo.salvar(_cliente(email="", receber_por_email=False))
    assert salvo.email == ""
    assert salvo.ativo is True


def test_as_duas_chaves_sao_independentes(repo):
    """Desligar o e-mail não pode tirar o cliente do faturamento."""
    cliente = repo.salvar(_cliente())
    repo.definir_chave(cliente.documento, "receber_por_email", False)

    recarregado = repo.buscar(cliente.documento)
    assert recarregado.receber_por_email is False
    assert recarregado.ativo is True, "desligar e-mail não pode desativar o cliente"

    repo.definir_chave(cliente.documento, "ativo", False)
    assert repo.buscar(cliente.documento).ativo is False


def test_listar_filtra_por_ativo_e_por_busca(repo):
    repo.salvar(_cliente())
    repo.salvar(_cliente(documento="11444777000161", razao_social="Beta Serviços", ativo=False))

    assert len(repo.listar()) == 2
    assert len(repo.listar(apenas_ativos=True)) == 1
    assert repo.listar(busca="Beta")[0].razao_social == "Beta Serviços"
    assert repo.listar(busca="11222333")[0].razao_social.startswith("Cliente Alfa")


def test_atualizar_preserva_a_data_de_criacao(repo):
    criado = repo.salvar(_cliente())
    atualizado = repo.salvar(_cliente(razao_social="Cliente Alfa S/A"))
    assert atualizado.criado_em == criado.criado_em
    assert repo.buscar(criado.documento).razao_social == "Cliente Alfa S/A"


def test_exportar_e_importar_leva_as_chaves_junto(repo, tmp_path):
    repo.salvar(_cliente(receber_por_email=False, email=""))
    repo.salvar(_cliente(documento="11444777000161", razao_social="Beta", ativo=False,
                         email="", receber_por_email=False))
    exportado = json.loads(json.dumps(repo.exportar()))

    outro = RepositorioClientes(BancoLocal(tmp_path / "outro.db"))
    criados, atualizados, erros = outro.importar(exportado)

    assert (criados, atualizados, erros) == (2, 0, [])
    assert outro.buscar("11222333000181").receber_por_email is False
    assert outro.buscar("11444777000161").ativo is False

    # Reimportar atualiza, não duplica.
    criados, atualizados, _ = outro.importar(exportado)
    assert (criados, atualizados) == (0, 2)


def test_importacao_reporta_registros_ruins_sem_abortar(repo):
    criados, _, erros = repo.importar([
        {"documento": "11222333000181", "razao_social": "Boa", "email": "a@b.com"},
        {"documento": "00000000000000", "razao_social": "Ruim"},
    ])
    assert criados == 1
    assert len(erros) == 1 and "Ruim" in erros[0]


# ---------------------------------------------------------------------------
# Ponte com a emissão
# ---------------------------------------------------------------------------
def _linha(**sobrepor) -> LinhaFaturamento:
    base = dict(
        numero_linha=2, documento_tomador="11222333000181", razao_social="Cliente Alfa",
        email="", valor_servico=Decimal("1000.00"), descricao="Consultoria.", extras={},
    )
    base.update(sobrepor)
    return LinhaFaturamento(**base)


def test_cadastro_completa_o_que_falta_na_planilha(repo):
    """Endereço incompleto do tomador é causa comum de rejeição."""
    cliente = repo.salvar(_cliente())
    linha, completados = completar_com_cadastro(_linha(), cliente)

    assert linha.extras["Logradouro"] == "Avenida Rio Branco"
    assert linha.extras["Cod_Municipio"] == "3304557"
    assert linha.email == "financeiro@alfa.com.br"
    assert "Email_Cliente" in completados


def test_planilha_tem_precedencia_sobre_o_cadastro(repo):
    """Se a pessoa digitou algo na planilha, foi de propósito."""
    cliente = repo.salvar(_cliente())
    linha = _linha(email="outro@cliente.com.br", extras={"Logradouro": "Rua da Planilha"})

    linha, completados = completar_com_cadastro(linha, cliente)

    assert linha.extras["Logradouro"] == "Rua da Planilha"
    assert linha.email == "outro@cliente.com.br"
    assert "Logradouro" not in completados


def test_chave_do_cadastro_define_o_envio(repo):
    cliente = repo.salvar(_cliente(receber_por_email=False))
    linha, _ = completar_com_cadastro(_linha(), cliente)
    assert linha.enviar_email is False

    ativo = repo.salvar(_cliente(documento="11444777000161", razao_social="Beta",
                                 email="b@beta.com.br"))
    linha2, _ = completar_com_cadastro(_linha(documento_tomador="11444777000161"), ativo)
    assert linha2.enviar_email is True


def test_cliente_desconhecido_nao_altera_a_linha():
    linha, completados = completar_com_cadastro(_linha(), None)
    assert completados == []
    assert linha.enviar_email is True


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------
def test_historico_registra_e_busca(emissoes):
    emissoes.registrar({
        "chave_acesso": "CHAVE1", "documento_tomador": "11222333000181",
        "tomador": "Cliente Alfa", "valor_servico": "1000.00",
        "emitida_em": "2026-09-01T10:00:00", "pasta": "/notas/2026/09-setembro",
    })
    assert len(emissoes.listar()) == 1
    assert emissoes.listar(busca="Alfa")[0]["chave_acesso"] == "CHAVE1"
    assert emissoes.listar(busca="inexistente") == []


def test_historico_sem_chave_e_ignorado(emissoes):
    emissoes.registrar({"tomador": "Sem chave"})
    assert emissoes.listar() == []


def test_reconstruir_le_os_arquivos_em_disco(emissoes, tmp_path):
    """O índice é descartável: os arquivos da pasta de notas é que são a verdade."""
    pasta = tmp_path / "notas" / "2026" / "09-setembro"
    pasta.mkdir(parents=True)
    for chave in ("CHAVE1", "CHAVE2"):
        (pasta / f"{chave}_11222333000181_retorno.json").write_text(
            json.dumps({
                "chave_acesso": chave, "tomador": "Cliente Alfa",
                "documento_tomador": "11222333000181", "valor_servico": "1000.00",
                "emitida_em": "2026-09-01T10:00:00",
            }),
            encoding="utf-8",
        )

    emissoes.registrar({"chave_acesso": "FANTASMA", "tomador": "Não existe em disco"})
    total = emissoes.reconstruir(tmp_path / "notas")

    assert total == 2
    chaves = {n["chave_acesso"] for n in emissoes.listar()}
    assert chaves == {"CHAVE1", "CHAVE2"}, "o registro sem arquivo deve sumir"


def test_reconstruir_pasta_inexistente_nao_quebra(emissoes, tmp_path):
    assert emissoes.reconstruir(tmp_path / "nao-existe") == 0
