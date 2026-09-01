"""Testes do armazenamento de configuração usado pela interface gráfica."""

from __future__ import annotations

import json
from decimal import Decimal

import keyring
import pytest
from keyring.backend import KeyringBackend

from nfse import armazenamento_config as ac


class CofreEmMemoria(KeyringBackend):
    """Backend de keyring para teste — não toca o cofre real do sistema."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.dados: dict[tuple[str, str], str] = {}

    def set_password(self, servico, usuario, senha):
        self.dados[(servico, usuario)] = senha

    def get_password(self, servico, usuario):
        return self.dados.get((servico, usuario))

    def delete_password(self, servico, usuario):
        self.dados.pop((servico, usuario), None)


@pytest.fixture(autouse=True)
def ambiente_isolado(tmp_path, monkeypatch):
    """Cada teste usa sua própria pasta de dados e seu próprio cofre."""
    monkeypatch.setenv("EMISSOR_NFSE_DIR", str(tmp_path / "dados"))
    cofre = CofreEmMemoria()
    anterior = keyring.get_keyring()
    keyring.set_keyring(cofre)
    yield cofre
    keyring.set_keyring(anterior)


def _config_valida(tmp_path) -> ac.ConfiguracaoApp:
    pfx = tmp_path / "certificado.pfx"
    pfx.write_bytes(b"fake")
    return ac.ConfiguracaoApp(
        prestador_cnpj="11222333000181",
        caminho_certificado_pfx=str(pfx),
        iss_aliquota="2.00",
        email_enviar=True,
        email_remetente="eu@empresa.com.br",
        email_bcc=["contador@escritorio.com.br"],
    )


def test_ida_e_volta_do_config_json(tmp_path):
    original = _config_valida(tmp_path)
    original.serie_dps = "3"
    original.servico_ctribnac = "170102"

    caminho = ac.salvar(original)
    recarregado = ac.carregar()

    assert caminho.exists()
    assert recarregado == original
    # Nenhuma senha pode aparecer no arquivo.
    assert "senha" not in caminho.read_text(encoding="utf-8").lower()


def test_config_inexistente_devolve_padroes():
    config = ac.carregar()
    assert config.ambiente == "homologacao"          # nunca produção por padrão
    assert config.prestador_cod_municipio == "3304557"
    assert config.email_enviar is False


def test_chaves_desconhecidas_sao_ignoradas(tmp_path):
    """Config gravado por uma versão mais nova não pode quebrar o app."""
    ac.salvar(_config_valida(tmp_path))
    caminho = ac.caminho_config()
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    dados["campo_de_versao_futura"] = "algo"
    caminho.write_text(json.dumps(dados), encoding="utf-8")

    assert ac.carregar().prestador_cnpj == "11222333000181"


def test_config_corrompido_da_erro_explicativo():
    ac.caminho_config().write_text("{isto não é json", encoding="utf-8")
    with pytest.raises(ac.ErroConfiguracaoApp, match="apague o arquivo"):
        ac.carregar()


def test_senhas_vao_para_o_cofre_e_nao_para_o_disco(ambiente_isolado, tmp_path):
    ac.guardar_senha(ac.CHAVE_SENHA_CERTIFICADO, "senha-do-pfx")
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "senha-de-app")

    assert ac.ler_senha(ac.CHAVE_SENHA_CERTIFICADO) == "senha-do-pfx"
    assert ac.ler_senha(ac.CHAVE_SENHA_SMTP) == "senha-de-app"
    assert ("EmissorNFSe", ac.CHAVE_SENHA_CERTIFICADO) in ambiente_isolado.dados

    ac.salvar(_config_valida(tmp_path))
    conteudo = ac.caminho_config().read_text(encoding="utf-8")
    assert "senha-do-pfx" not in conteudo
    assert "senha-de-app" not in conteudo


def test_senha_vazia_remove_do_cofre():
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "algo")
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "")
    assert ac.ler_senha(ac.CHAVE_SENHA_SMTP) is None


def test_conversao_para_o_dataclass_do_nucleo(tmp_path):
    """A interface e o .env têm que produzir o mesmo `Configuracao`."""
    app = _config_valida(tmp_path)
    app.ambiente = "producao"
    ac.guardar_senha(ac.CHAVE_SENHA_CERTIFICADO, "senha-do-pfx")

    config = app.para_configuracao()

    assert config.tp_amb == 1
    assert config.url_base == "https://sefin.nfse.gov.br/sefinnacional"
    assert config.prestador.cnpj == "11222333000181"
    assert config.servico.aliquota_iss == Decimal("2.00")
    assert config.senha_pfx == "senha-do-pfx"
    assert config.usa_pfx is True


def test_aliquota_vazia_vira_none(tmp_path):
    """Optante do Simples normalmente não informa pAliq na DPS."""
    app = _config_valida(tmp_path)
    app.iss_aliquota = ""
    assert app.para_configuracao().servico.aliquota_iss is None


def test_conversao_para_configuracao_de_email(tmp_path):
    app = _config_valida(tmp_path)
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "senha-de-app")

    email = app.para_configuracao_email()

    assert email.ativo is True
    assert email.senha == "senha-de-app"
    assert email.copia_oculta == ["contador@escritorio.com.br"]
    assert email.corpo_modelo.strip(), "corpo vazio deve cair no modelo padrão"


def test_pendencias_apontam_o_que_falta(tmp_path):
    vazia = ac.ConfiguracaoApp()
    pendencias = " ".join(vazia.pendencias())
    assert "CNPJ do prestador" in pendencias
    assert "Certificado Digital A1" in pendencias

    assert _config_valida(tmp_path).pendencias() == []


def test_pendencia_de_certificado_ausente(tmp_path):
    app = _config_valida(tmp_path)
    app.caminho_certificado_pfx = str(tmp_path / "sumiu.pfx")
    assert any("não encontrado" in p for p in app.pendencias())


def test_carregar_do_app_recusa_configuracao_incompleta():
    with pytest.raises(ac.ErroConfiguracaoApp, match="Configuração incompleta"):
        ac.carregar_do_app()
