"""Testes da interface web local — travas de segurança e fluxo de emissão."""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import keyring
import pandas as pd
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.recorrencias import repositorio_recorrencias, verificar_pendencias
from app.seguranca import NOME_HEADER, Guardiao
from app.servidor import criar_app
from app.sessao import ESTADO, TrabalhoEmissao
from nfse import armazenamento_config as ac
from nfse.certificado import carregar_certificado
from nfse.cliente import RespostaEmissao
from nfse.estado import ControleEmissao
from nfse.servico import Emissor
from tests.test_config_app import CofreEmMemoria

TOKEN = "token-fixo-de-teste"


@pytest.fixture
def dados_app(tmp_path, monkeypatch):
    """Pasta de dados isolada, cofre em memória e certificado de teste."""
    monkeypatch.setenv("EMISSOR_NFSE_DIR", str(tmp_path / "dados"))
    anterior = keyring.get_keyring()
    keyring.set_keyring(CofreEmMemoria())

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EMPRESA TESTE:11222333000181")])
    agora = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(nome).issuer_name(nome)
        .public_key(chave.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(days=1))
        .not_valid_after(agora + dt.timedelta(days=180))
        .sign(chave, hashes.SHA256())
    )
    pfx = tmp_path / "certificado.pfx"
    pfx.write_bytes(pkcs12.serialize_key_and_certificates(
        b"teste", chave, cert, None, serialization.BestAvailableEncryption(b"senha123")
    ))

    ac.salvar(ac.ConfiguracaoApp(
        prestador_cnpj="11222333000181",
        servico_ptottribsn="8.63",
        caminho_certificado_pfx=str(pfx),
        diretorio_notas=str(tmp_path / "notas"),
        diretorio_logs=str(tmp_path / "logs"),
    ))
    ac.guardar_senha(ac.CHAVE_SENHA_CERTIFICADO, "senha123")

    # Estado global limpo entre testes.
    ESTADO.importacao = None
    ESTADO.trabalho = TrabalhoEmissao()

    yield tmp_path
    keyring.set_keyring(anterior)


@pytest.fixture
def cliente(dados_app) -> TestClient:
    # base_url em 127.0.0.1: o padrão do TestClient é "testserver", que o
    # middleware barra de propósito (só o próprio computador é atendido).
    return TestClient(criar_app(Guardiao(TOKEN)), base_url="http://127.0.0.1:8765")


def _planilha(linhas: list[dict]) -> bytes:
    buffer = io.BytesIO()
    pd.DataFrame(linhas).to_excel(buffer, index=False)
    return buffer.getvalue()


LINHA_BOA = {
    "CNPJ_Cliente": "11.222.333/0001-81",
    "Razao_Social": "Cliente Alfa",
    "Email_Cliente": "alfa@exemplo.com.br",
    "Valor_Servico": "1000,00",
    "Descricao_Servico": "Consultoria estratégica.",
}
LINHA_RUIM = {**LINHA_BOA, "CNPJ_Cliente": "11222333000100", "Razao_Social": "Cliente Beta"}


# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------
def test_rota_que_altera_estado_exige_token(cliente):
    """Sem esta trava, qualquer site aberto no navegador emitiria nota."""
    resposta = cliente.post("/emitir/iniciar", data={"competencia": "2026-09"})
    assert resposta.status_code == 403
    assert "Sessão inválida" in resposta.json()["erro"]


def test_token_errado_e_recusado(cliente):
    resposta = cliente.post(
        "/configuracao/testar-certificado", headers={NOME_HEADER: "chute"}
    )
    assert resposta.status_code == 403


def test_leitura_nao_exige_token(cliente):
    assert cliente.get("/").status_code == 200
    assert cliente.get("/historico").status_code == 200


def test_host_externo_e_recusado(cliente):
    """Defesa contra DNS rebinding: só o próprio computador é atendido."""
    resposta = cliente.get("/", headers={"Host": "site-malicioso.com"})
    assert resposta.status_code == 403
    assert "só responde ao próprio computador" in resposta.text


def test_token_na_url_fixa_o_cookie(cliente):
    resposta = cliente.get(f"/?t={TOKEN}", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.cookies.get("emissor_token") == TOKEN


# ---------------------------------------------------------------------------
# Importação
# ---------------------------------------------------------------------------
def test_importacao_mostra_linhas_validas_e_invalidas(cliente):
    resposta = cliente.post(
        "/importar",
        headers={NOME_HEADER: TOKEN},
        files={"planilha": ("faturamento.xlsx", _planilha([LINHA_BOA, LINHA_RUIM]))},
        follow_redirects=False,
    )
    assert resposta.status_code == 303

    assert ESTADO.importacao is not None
    assert len(ESTADO.importacao.validas) == 1
    assert len(ESTADO.importacao.invalidas) == 1
    assert "dígito verificador" in ESTADO.importacao.invalidas[0].erro

    # A grade de pré-visualização traz o motivo exato do problema.
    pagina = cliente.get("/importar").text
    assert "dígito verificador" in pagina
    assert "Cliente Alfa" in pagina


def test_planilha_sem_coluna_obrigatoria_e_recusada(cliente):
    resposta = cliente.post(
        "/importar",
        headers={NOME_HEADER: TOKEN},
        files={"planilha": ("ruim.xlsx", _planilha([{"CNPJ_Cliente": "1"}]))},
    )
    assert resposta.status_code == 200
    assert "Razao_Social" in resposta.text
    assert ESTADO.importacao is None


# ---------------------------------------------------------------------------
# Emissão
# ---------------------------------------------------------------------------
def _importar(cliente, linhas=None):
    cliente.post(
        "/importar",
        headers={NOME_HEADER: TOKEN},
        files={"planilha": ("faturamento.xlsx", _planilha(linhas or [LINHA_BOA]))},
    )


def test_emissao_sem_planilha_e_recusada(cliente):
    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "2026-09", "modo": "simular"},
    )
    assert resposta.status_code == 400
    assert "Importe uma planilha" in resposta.json()["mensagem"]


def test_simulacao_gera_o_xml_sem_transmitir(cliente, dados_app):
    _importar(cliente)
    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "2026-09", "modo": "simular", "linhas": "2"},
    )
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True, "total": 1, "dry_run": True}

    # O fluxo SSE termina quando o lote acaba — consumi-lo aguarda a thread.
    eventos = cliente.get("/emitir/eventos").text
    assert "inicio_lote" in eventos
    assert "DRY-RUN" in eventos

    simulados = list((dados_app / "notas" / "dry-run").glob("*.xml"))
    assert len(simulados) == 1
    assert b"<DPS" in simulados[0].read_bytes()
    assert ESTADO.trabalho.estado == "concluido"


def test_producao_exige_a_frase_de_confirmacao(cliente):
    config = ac.carregar()
    config.ambiente = "producao"
    ac.salvar(config)
    _importar(cliente)

    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "2026-09", "modo": "emitir", "linhas": "2", "confirmacao": "sim"},
    )
    assert resposta.status_code == 400
    assert "EMITIR EM PRODUCAO" in resposta.json()["mensagem"]
    assert ESTADO.trabalho.estado == "ocioso", "nada pode ter sido transmitido"


def test_configuracao_incompleta_bloqueia_a_emissao(cliente):
    _importar(cliente)
    ac.salvar(ac.ConfiguracaoApp())   # sem CNPJ nem certificado

    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "2026-09", "modo": "simular", "linhas": "2"},
    )
    assert resposta.status_code == 400
    assert "Configuração incompleta" in resposta.json()["mensagem"]


def test_dois_lotes_simultaneos_sao_recusados(cliente):
    """Numeração da DPS não suporta concorrência."""
    _importar(cliente)
    ESTADO.trabalho.estado = "rodando"

    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "2026-09", "modo": "simular", "linhas": "2"},
    )
    assert resposta.status_code == 409
    assert "em andamento" in resposta.json()["mensagem"]


def test_competencia_invalida_e_recusada(cliente):
    _importar(cliente)
    resposta = cliente.post(
        "/emitir/iniciar",
        headers={NOME_HEADER: TOKEN},
        data={"competencia": "setembro", "modo": "simular", "linhas": "2"},
    )
    assert resposta.status_code == 400
    assert "Competência inválida" in resposta.json()["mensagem"]


# ---------------------------------------------------------------------------
# Configuração pela tela
# ---------------------------------------------------------------------------
def test_salvar_configuracao_guarda_senha_no_cofre_e_nao_no_disco(cliente):
    resposta = cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={
            "ambiente": "homologacao",
            "prestador_cnpj": "11.222.333/0001-81",
            "serie_dps": "2",
            "senha_smtp": "senha-secreta",
            "email_smtp_servidor": "smtp.exemplo.com",
        },
        follow_redirects=False,
    )
    assert resposta.status_code == 303

    config = ac.carregar()
    assert config.prestador_cnpj == "11222333000181", "máscara do CNPJ deve ser removida"
    assert config.serie_dps == "2"
    assert ac.ler_senha(ac.CHAVE_SENHA_SMTP) == "senha-secreta"
    assert "senha-secreta" not in ac.caminho_config().read_text(encoding="utf-8")


def test_senha_em_branco_mantem_a_atual(cliente):
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "senha-antiga")
    cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181", "senha_smtp": ""},
    )
    assert ac.ler_senha(ac.CHAVE_SENHA_SMTP) == "senha-antiga"


def test_teste_de_certificado_reporta_validade(cliente):
    resposta = cliente.post("/configuracao/testar-certificado", headers={NOME_HEADER: TOKEN})
    dados = resposta.json()
    assert dados["ok"] is True
    assert "EMPRESA TESTE" in dados["titular"]
    assert dados["dias"] > 170


def test_testar_conexao_relata_a_falha_sem_derrubar_a_tela(cliente, monkeypatch):
    """Prova o endereço da Sefin e o handshake sem emitir nada — é o que dá para
    verificar antes de virar a chave para produção, já que a homologação não
    exercita nem a URL de produção nem o certificado contra ela.

    Aqui a rede não existe, então o teste cobre o caminho da falha: tem que
    virar mensagem na tela, não erro 500.
    """
    from nfse import certificado as modulo_certificado

    class SessaoQueNaoConecta:
        def get(self, *args, **kwargs):
            raise OSError("nome do servidor não resolvido")

    monkeypatch.setattr(modulo_certificado, "criar_sessao_mtls", lambda cert: SessaoQueNaoConecta())
    monkeypatch.setattr("app.servidor.criar_sessao_mtls", lambda cert: SessaoQueNaoConecta())

    resposta = cliente.post("/configuracao/testar-conexao", headers={NOME_HEADER: TOKEN})

    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["ok"] is False
    assert "nome do servidor não resolvido" in dados["mensagem"]


def test_testar_conexao_confirma_quando_a_sefin_responde(cliente, monkeypatch):
    class RespostaFalsa:
        status_code = 404   # chave inexistente: já prova que passou do TLS

    monkeypatch.setattr(
        "app.servidor.criar_sessao_mtls",
        lambda cert: type("S", (), {"get": lambda self, *a, **k: RespostaFalsa()})(),
    )

    dados = cliente.post("/configuracao/testar-conexao", headers={NOME_HEADER: TOKEN}).json()

    assert dados["ok"] is True
    assert "certificado foi aceito" in dados["mensagem"]


def test_testar_certificado_funciona_antes_de_salvar(cliente, dados_app):
    """Não pode exigir 'Salvar' primeiro só para poder testar o arquivo escolhido."""
    config = ac.carregar()
    config.caminho_certificado_pfx = ""   # nada salvo ainda
    ac.salvar(config)
    ac.remover_senha(ac.CHAVE_SENHA_CERTIFICADO)

    conteudo = (dados_app / "certificado.pfx").read_bytes()
    resposta = cliente.post(
        "/configuracao/testar-certificado",
        headers={NOME_HEADER: TOKEN},
        files={"certificado_arquivo": ("meu-a1.pfx", conteudo)},
        data={"senha_certificado": "senha123"},
    )

    dados = resposta.json()
    assert dados["ok"] is True
    assert "EMPRESA TESTE" in dados["titular"]
    # Testar não pode ter salvo nada de permanente.
    assert ac.carregar().caminho_certificado_pfx == ""
    assert list((dados_app / "dados").glob("teste-certificado-*.pfx")) == []


def test_testar_certificado_sem_nada_da_mensagem_clara(cliente):
    config = ac.carregar()
    config.caminho_certificado_pfx = ""
    ac.salvar(config)

    resposta = cliente.post("/configuracao/testar-certificado", headers={NOME_HEADER: TOKEN})
    dados = resposta.json()
    assert dados["ok"] is False
    assert "Escolha o arquivo" in dados["mensagem"]


# ---------------------------------------------------------------------------
# Cadastro de clientes pela interface
# ---------------------------------------------------------------------------
def _cadastrar(cliente, **campos):
    dados = {
        "documento": "11.222.333/0001-81",
        "razao_social": "Cliente Alfa",
        "email": "alfa@exemplo.com.br",
        "cod_municipio": "3304557",
        "logradouro": "Avenida Rio Branco",
        "numero": "156",
        "bairro": "Centro",
        "cep": "20040901",
        "ativo": "on",
        "receber_por_email": "on",
    }
    dados.update(campos)
    return cliente.post("/clientes", headers={NOME_HEADER: TOKEN}, data=dados,
                        follow_redirects=False)


def test_cadastrar_cliente_pela_tela(cliente):
    from app.rotas_clientes import repositorio_clientes

    assert _cadastrar(cliente).status_code == 303
    salvo = repositorio_clientes().buscar("11222333000181")
    assert salvo.razao_social == "Cliente Alfa"
    assert salvo.ativo is True and salvo.receber_por_email is True

    assert "Cliente Alfa" in cliente.get("/clientes").text


def test_cadastro_invalido_volta_para_o_formulario(cliente):
    resposta = _cadastrar(cliente, documento="11222333000100")
    assert resposta.status_code == 200
    assert "dígito verificador" in resposta.text


def test_alternar_as_chaves_pela_lista(cliente):
    from app.rotas_clientes import repositorio_clientes

    _cadastrar(cliente)
    resposta = cliente.post(
        "/clientes/chave/11222333000181",
        headers={NOME_HEADER: TOKEN},
        data={"coluna": "receber_por_email", "valor": "0"},
    )
    assert resposta.json()["ok"] is True

    salvo = repositorio_clientes().buscar("11222333000181")
    assert salvo.receber_por_email is False
    assert salvo.ativo is True, "desligar o e-mail não pode desativar o cliente"


def test_ligar_email_sem_endereco_e_recusado(cliente):
    _cadastrar(cliente, email="", receber_por_email="")
    resposta = cliente.post(
        "/clientes/chave/11222333000181",
        headers={NOME_HEADER: TOKEN},
        data={"coluna": "receber_por_email", "valor": "1"},
    )
    assert resposta.status_code == 400
    assert "Cadastre um e-mail" in resposta.json()["mensagem"]


def test_coluna_nao_alternavel_e_recusada(cliente):
    """Guarda contra injeção de nome de coluna na consulta SQL."""
    _cadastrar(cliente)
    resposta = cliente.post(
        "/clientes/chave/11222333000181",
        headers={NOME_HEADER: TOKEN},
        data={"coluna": "razao_social", "valor": "1"},
    )
    assert resposta.status_code == 400


def test_exportar_devolve_json_para_download(cliente):
    _cadastrar(cliente)
    resposta = cliente.get("/clientes/exportar")
    assert resposta.status_code == 200
    assert "attachment" in resposta.headers["content-disposition"]
    assert resposta.json()[0]["documento"] == "11222333000181"


def test_importacao_da_planilha_completa_pelo_cadastro(cliente):
    """Endereço que falta na planilha vem do cadastro."""
    _cadastrar(cliente)
    _importar(cliente)

    previa = ESTADO.importacao.validas[0]
    assert previa.linha.extras["Logradouro"] == "Avenida Rio Branco"
    assert previa.cliente_ativo is True
    assert "Logradouro" in previa.do_cadastro


def test_planilha_cadastra_clientes_novos(cliente):
    """A planilha vive só na memória; o cadastro é que sobrevive ao app fechar.
    Antes, os clientes "sumiam" ao trocar a pasta do programa."""
    from app.rotas_clientes import repositorio_clientes

    resposta = cliente.post(
        "/importar", headers={NOME_HEADER: TOKEN}, follow_redirects=False,
        files={"planilha": ("faturamento.xlsx", _planilha([LINHA_BOA]))},
    )
    assert resposta.headers["location"] == "/emitir?novos=1"

    salvo = repositorio_clientes().buscar("11222333000181")
    assert salvo is not None and salvo.razao_social == "Cliente Alfa"
    assert salvo.email == "alfa@exemplo.com.br" and salvo.receber_por_email is True
    assert "1 cliente(s) novo(s)" in cliente.get("/emitir?novos=1").text


def test_planilha_nao_sobrescreve_cliente_ja_cadastrado(cliente):
    from app.rotas_clientes import repositorio_clientes

    _cadastrar(cliente, razao_social="Nome ajustado à mão", email="certo@cliente.com.br")
    _importar(cliente)
    salvo = repositorio_clientes().buscar("11222333000181")
    assert salvo.razao_social == "Nome ajustado à mão"
    assert salvo.email == "certo@cliente.com.br"


# ---------------------------------------------------------------------------
# Nota avulsa
# ---------------------------------------------------------------------------
def _avulsa(cliente, **campos):
    dados = {
        "documento": "11222333000181",
        "competencia": "2026-09",
        "valor": "4.500,00",
        "descricao": "Consultoria estratégica.",
        "modo": "simular",
    }
    dados.update(campos)
    return cliente.post("/avulsa/emitir", headers={NOME_HEADER: TOKEN}, data=dados)


def test_nota_avulsa_simula_e_gera_o_xml(cliente, dados_app):
    _cadastrar(cliente)
    resposta = _avulsa(cliente)
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True, "total": 1, "dry_run": True}

    cliente.get("/emitir/eventos")   # consumir o SSE aguarda o lote terminar
    simulados = list((dados_app / "notas" / "dry-run").glob("*.xml"))
    assert len(simulados) == 1
    assert b"Consultoria" in simulados[0].read_bytes()


def test_nota_avulsa_recusa_cliente_inativo(cliente):
    _cadastrar(cliente, ativo="")
    resposta = _avulsa(cliente)
    assert resposta.status_code == 400
    assert "inativo" in resposta.json()["mensagem"]


def test_nota_avulsa_recusa_cliente_desconhecido(cliente):
    resposta = _avulsa(cliente, documento="11444777000161")
    assert resposta.status_code == 400
    assert "Escolha um cliente" in resposta.json()["mensagem"]


def test_nota_avulsa_valida_valor_e_descricao(cliente):
    _cadastrar(cliente)
    assert "Valor inválido" in _avulsa(cliente, valor="0").json()["mensagem"]
    assert "Valor inválido" in _avulsa(cliente, valor="abc").json()["mensagem"]
    assert "Descreva o serviço" in _avulsa(cliente, descricao="  ").json()["mensagem"]


def test_nota_avulsa_em_producao_exige_confirmacao(cliente):
    _cadastrar(cliente)
    config = ac.carregar()
    config.ambiente = "producao"
    ac.salvar(config)

    resposta = _avulsa(cliente, modo="emitir", confirmacao="sim")
    assert resposta.status_code == 400
    assert "EMITIR EM PRODUCAO" in resposta.json()["mensagem"]


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------
def test_historico_reconstroi_a_partir_dos_arquivos(cliente, dados_app):
    import json as _json

    pasta = dados_app / "notas" / "2026" / "09-setembro"
    pasta.mkdir(parents=True)
    (pasta / "CHAVE1_11222333000181_retorno.json").write_text(
        _json.dumps({
            "chave_acesso": "CHAVE1", "tomador": "Cliente Alfa",
            "documento_tomador": "11222333000181", "valor_servico": "1000.00",
            "emitida_em": "2026-09-01T10:00:00",
        }),
        encoding="utf-8",
    )

    resposta = cliente.post("/historico/reconstruir", headers={NOME_HEADER: TOKEN},
                            follow_redirects=True)
    assert resposta.status_code == 200
    assert "CHAVE1" in resposta.text
    assert "Cliente Alfa" in resposta.text


def test_formulario_funciona_so_com_o_cookie(cliente):
    """Caminho real do usuário: o cookie SameSite=Strict basta, sem header."""
    cliente.get(f"/?t={TOKEN}")              # o navegador recebe o cookie aqui
    resposta = cliente.post(
        "/clientes",
        data={"documento": "11222333000181", "razao_social": "Via cookie",
              "email": "a@b.com.br", "ativo": "on", "receber_por_email": "on"},
        follow_redirects=False,
    )
    assert resposta.status_code == 303, "POST de formulário do próprio app deve passar"


def test_configuracao_salva_mesmo_sem_cofre_de_senhas(cliente, monkeypatch):
    """O defeito era 500 e perda de tudo que a pessoa tinha preenchido."""
    monkeypatch.setattr(ac, "guardar_senha", lambda chave, senha: False)

    resposta = cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181", "serie_dps": "7",
              "senha_certificado": "senha-do-pfx"},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    assert "senha_volatil" in resposta.headers["location"]
    assert ac.carregar().serie_dps == "7", "o resto da configuração tem que ser salvo"


def test_tela_avisa_que_a_senha_e_so_da_sessao(cliente):
    pagina = cliente.get("/configuracao?salvo=true&senha_volatil=do%20certificado").text
    assert "não pôde ser guardada" in pagina
    assert "do certificado" in pagina


def test_upload_do_certificado_pela_tela(cliente, dados_app):
    """O upload vinha sendo descartado em silêncio por um isinstance errado."""
    conteudo = (dados_app / "certificado.pfx").read_bytes()

    resposta = cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181"},
        files={"certificado_arquivo": ("meu-a1.pfx", conteudo)},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    caminho = ac.carregar().caminho_certificado_pfx
    assert caminho, "o caminho do certificado precisa ficar salvo"
    assert Path(caminho).read_bytes() == conteudo
    assert ac.carregar().pendencias() == [], "com o certificado, não deve sobrar pendência"


def test_codigos_de_tributacao_colados_do_portal_sao_normalizados(cliente):
    """O portal mostra "17.03.03" e "17.03.03.001"; colado assim, o cTribNac
    com pontos virava E0310 ("código não existe") na Sefin."""
    cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181",
              "servico_ctribnac": "17.03.03", "servico_ctribmun": "17.03.03.001"},
        follow_redirects=False,
    )
    config = ac.carregar()
    assert config.servico_ctribnac == "170303"
    assert config.servico_ctribmun == "001"
    assert config.para_configuracao().servico.codigo_tributacao_municipal == "001"
    assert config.pendencias() == []


def test_percentual_do_simples_aceita_virgula_e_simbolo(cliente):
    cliente.post(
        "/configuracao", headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181", "servico_ptottribsn": "8,63%"},
        follow_redirects=False,
    )
    config = ac.carregar()
    assert config.servico_ptottribsn == "8.63"
    assert str(config.para_configuracao().servico.percentual_tributos_sn) == "8.63"


def test_historico_gera_e_abre_o_pdf_de_nota_ja_arquivada(cliente, dados_app, config, linha):
    """Nota autorizada sem PDF (a API da Sefin foi desligada): o botão PDF do
    Histórico gera o DANFSe a partir do XML arquivado."""
    from tests.conftest import CHAVE_EXEMPLO, montar_nfse_xml

    pasta = dados_app / "notas" / "2026" / "10-outubro"
    pasta.mkdir(parents=True)
    (pasta / f"{CHAVE_EXEMPLO}_11222333000181_nfse.xml").write_bytes(montar_nfse_xml(config, linha))

    resposta = cliente.get(f"/danfse/{CHAVE_EXEMPLO}")
    assert resposta.status_code == 200
    assert resposta.headers["content-type"] == "application/pdf"
    assert resposta.content[:4] == b"%PDF"
    assert (pasta / f"{CHAVE_EXEMPLO}_11222333000181_danfse.pdf").exists()

    assert cliente.get("/danfse/0000").status_code == 404
    assert cliente.get("/danfse/..%2F..%2Fsegredo").status_code in (400, 404)


def test_serie_do_emissor_web_vira_pendencia(dados_app):
    """Rejeição real E0010: série 70000 é a faixa do emissor web do portal;
    aplicativo próprio só pode usar 1-49999."""
    config = ac.carregar()
    config.serie_dps = "70000"
    assert any("fora da faixa" in p for p in config.pendencias())
    config.serie_dps = "1"
    assert config.pendencias() == []


def test_configuracao_sem_novo_certificado_mantem_o_atual(cliente):
    anterior = ac.carregar().caminho_certificado_pfx
    cliente.post(
        "/configuracao",
        headers={NOME_HEADER: TOKEN},
        data={"prestador_cnpj": "11222333000181", "serie_dps": "9"},
        follow_redirects=False,
    )
    config = ac.carregar()
    assert config.caminho_certificado_pfx == anterior
    assert config.serie_dps == "9"


# ---------------------------------------------------------------------------
# Recorrências
# ---------------------------------------------------------------------------
class _ClienteNFSeFalso:
    """Substitui `ClienteNFSe` sem tocar a rede — mesma técnica de test_servico.py."""

    def __init__(self, respostas: list[RespostaEmissao]):
        self.respostas = list(respostas)

    def emitir(self, pacote: str) -> RespostaEmissao:
        return self.respostas.pop(0)


def _emissor_falso(config_app: ac.ConfiguracaoApp, chave: str = "CHAVE-REC1") -> Emissor:
    configuracao = config_app.para_configuracao()
    certificado = carregar_certificado(configuracao)
    controle = ControleEmissao.carregar(
        configuracao.diretorio_logs, config_app.ambiente, config_app.serie_dps,
        config_app.numero_dps_inicial,
    )
    resposta = RespostaEmissao(
        autorizada=True, status_http=201, chave_acesso=chave, id_dps="DPS" + chave,
        xml_nfse=b"<NFSe/>", mensagens=[],
    )
    return Emissor(
        config=configuracao, config_email=config_app.para_configuracao_email(),
        certificado=certificado, cliente=_ClienteNFSeFalso([resposta]), controle=controle,
    )


def _cliente_recorrente(cliente, **campos):
    dados = {"dia_emissao_recorrente": "1", "descricao_recorrente": "Consultoria mensal.",
             "valor_recorrente": "1.900,00"}
    dados.update(campos)
    return _cadastrar(cliente, **dados)


def test_tela_recorrencias_vazia(cliente):
    pagina = cliente.get("/recorrencias", headers={NOME_HEADER: TOKEN}).text
    assert "Nenhuma pendência agora" in pagina


def test_verificar_pendencias_cria_pendencia_para_cliente_vencido(cliente):
    _cliente_recorrente(cliente)
    criadas = verificar_pendencias(ac.carregar())
    assert len(criadas) == 1
    assert criadas[0].estado == "aguardando_aprovacao"
    assert criadas[0].valor == "1900.00"

    pagina = cliente.get("/recorrencias", headers={NOME_HEADER: TOKEN}).text
    assert "Cliente Alfa" in pagina


def test_verificar_pendencias_sem_valor_fica_aguardando_valor(cliente):
    _cliente_recorrente(cliente, valor_recorrente="")
    criadas = verificar_pendencias(ac.carregar())
    assert criadas[0].estado == "aguardando_valor"


def test_verificar_pendencias_e_idempotente(cliente):
    _cliente_recorrente(cliente)
    verificar_pendencias(ac.carregar())
    verificar_pendencias(ac.carregar())
    assert len(repositorio_recorrencias().listar_pendentes()) == 1


def test_emitir_recorrencia_inexistente_e_recusada(cliente):
    resposta = cliente.post("/recorrencias/999/emitir", headers={NOME_HEADER: TOKEN}, data={})
    assert resposta.status_code == 404


def test_emitir_recorrencia_com_valor_invalido_e_recusada(cliente):
    _cliente_recorrente(cliente, valor_recorrente="")
    pendencia = verificar_pendencias(ac.carregar())[0]

    resposta = cliente.post(f"/recorrencias/{pendencia.id}/emitir", headers={NOME_HEADER: TOKEN},
                            data={"valor": "abc"})
    assert resposta.status_code == 400
    assert "Valor inválido" in resposta.json()["mensagem"]


def test_emitir_recorrencia_em_producao_exige_confirmacao(cliente):
    _cliente_recorrente(cliente)
    pendencia = verificar_pendencias(ac.carregar())[0]
    config = ac.carregar()
    config.ambiente = "producao"
    ac.salvar(config)

    resposta = cliente.post(f"/recorrencias/{pendencia.id}/emitir", headers={NOME_HEADER: TOKEN}, data={})
    assert resposta.status_code == 400
    assert "EMITIR EM PRODUCAO" in resposta.json()["mensagem"]


def test_emitir_recorrencia_autoriza_e_marca_emitida(cliente, monkeypatch):
    _cliente_recorrente(cliente)
    pendencia = verificar_pendencias(ac.carregar())[0]
    monkeypatch.setattr("app.recorrencias.montar_emissor",
                        lambda *a, **k: _emissor_falso(ac.carregar()))

    resposta = cliente.post(f"/recorrencias/{pendencia.id}/emitir", headers={NOME_HEADER: TOKEN}, data={})
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True}

    cliente.get("/emitir/eventos")   # consumir o SSE aguarda o lote terminar

    resolvida = repositorio_recorrencias().buscar(pendencia.id)
    assert resolvida.estado == "emitida"
    assert resolvida.chave_acesso == "CHAVE-REC1"
    assert repositorio_recorrencias().listar_pendentes() == []


def test_emitir_recorrencia_aguardando_valor_aceita_o_valor_digitado(cliente, monkeypatch):
    _cliente_recorrente(cliente, valor_recorrente="")
    pendencia = verificar_pendencias(ac.carregar())[0]
    assert pendencia.estado == "aguardando_valor"
    monkeypatch.setattr("app.recorrencias.montar_emissor",
                        lambda *a, **k: _emissor_falso(ac.carregar()))

    resposta = cliente.post(f"/recorrencias/{pendencia.id}/emitir", headers={NOME_HEADER: TOKEN},
                            data={"valor": "3.500,00"})
    assert resposta.status_code == 200
    cliente.get("/emitir/eventos")

    resolvida = repositorio_recorrencias().buscar(pendencia.id)
    assert resolvida.estado == "emitida"
    assert resolvida.valor == "3500.00"


def test_pular_recorrencia_some_das_pendentes(cliente):
    _cliente_recorrente(cliente)
    pendencia = verificar_pendencias(ac.carregar())[0]

    resposta = cliente.post(f"/recorrencias/{pendencia.id}/pular", headers={NOME_HEADER: TOKEN}, data={})
    assert resposta.status_code == 200
    assert repositorio_recorrencias().listar_pendentes() == []
    assert repositorio_recorrencias().buscar(pendencia.id).estado == "pulada"


def test_modo_automatico_emite_sozinho_ao_verificar(cliente, monkeypatch):
    _cliente_recorrente(cliente)
    config = ac.carregar()
    config.recorrencia_modo = "automatico"
    ac.salvar(config)
    monkeypatch.setattr("app.recorrencias.montar_emissor",
                        lambda *a, **k: _emissor_falso(ac.carregar()))

    verificar_pendencias(ac.carregar())
    cliente.get("/emitir/eventos")   # consumir o SSE aguarda o lote terminar

    pendentes = repositorio_recorrencias().listar_pendentes()
    assert pendentes == [], "modo automático deveria emitir sozinho, sem esperar clique"


def test_modo_manual_nao_emite_sozinho_ao_verificar(cliente):
    _cliente_recorrente(cliente)
    verificar_pendencias(ac.carregar())  # recorrencia_modo padrão é "manual"

    pendentes = repositorio_recorrencias().listar_pendentes()
    assert len(pendentes) == 1, "modo manual espera o clique em Emitir agora"


def test_reenviar_email_de_nota_ja_emitida(cliente, dados_app, config, linha, monkeypatch):
    """Nota autorizada antes de existir o PDF local: reenvia PDF + XML ao
    e-mail que está na própria nota."""
    from tests.conftest import CHAVE_EXEMPLO, montar_nfse_xml

    pasta = dados_app / "notas" / "2026" / "10-outubro"
    pasta.mkdir(parents=True)
    (pasta / f"{CHAVE_EXEMPLO}_11222333000181_nfse.xml").write_bytes(montar_nfse_xml(config, linha))

    conf = ac.carregar()
    conf.email_enviar = True
    conf.email_smtp_usuario = conf.email_remetente = "eu@empresa.com.br"
    conf.email_permitir_homologacao = True
    ac.salvar(conf)
    ac.guardar_senha(ac.CHAVE_SENHA_SMTP, "senha-de-app")
    entregues = []
    monkeypatch.setattr("nfse.email_envio._entregar", lambda c, msg, destino: entregues.append((msg, destino)))

    resposta = cliente.post(f"/historico/reenviar/{CHAVE_EXEMPLO}", headers={NOME_HEADER: TOKEN})

    assert resposta.json()["ok"] is True, resposta.json()
    mensagem, destino = entregues[0]
    assert destino == "financeiro@clientealfa.com.br"   # o e-mail que está na nota
    anexos = [a.get_filename() for a in mensagem.iter_attachments()]
    assert any(a.endswith("_danfse.pdf") for a in anexos)
    assert any(a.endswith("_nfse.xml") for a in anexos)


def test_reenviar_email_sem_xml_avisa(cliente):
    resposta = cliente.post("/historico/reenviar/NAOEXISTE", headers={NOME_HEADER: TOKEN})
    assert resposta.json()["ok"] is False
    assert "não está na pasta de notas" in resposta.json()["mensagem"]


@pytest.mark.parametrize("nome, conteudo", [
    ("planilha.xlsx", b"CNPJ_Cliente,Razao_Social\n1,2\n"),   # CSV com extensão trocada
    ("planilha.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600),  # Excel 97-2003
    ("planilha.xlsx", b""),
])
def test_planilha_em_outro_formato_explica_em_vez_de_erro_500(cliente, nome, conteudo):
    """Caso real: a planilha corrigida voltou com erro 500 ao importar."""
    resposta = cliente.post("/importar", headers={NOME_HEADER: TOKEN},
                            files={"planilha": (nome, conteudo)})
    assert resposta.status_code == 200
    assert "Salvar como" in resposta.text and ".xlsx" in resposta.text


def test_erro_inesperado_vai_para_o_log(dados_app, monkeypatch):
    def quebra(*a, **k):
        raise RuntimeError("falha de teste")

    monkeypatch.setattr("app.servidor.importar_planilha", quebra)
    cliente = TestClient(criar_app(Guardiao(TOKEN)), base_url="http://127.0.0.1:8765",
                         raise_server_exceptions=False)
    resposta = cliente.post("/importar", headers={NOME_HEADER: TOKEN},
                            files={"planilha": ("p.xlsx", _planilha([LINHA_BOA]))})

    assert resposta.status_code == 500
    assert "erros.log" in resposta.text
    log = (ac.diretorio_dados() / "erros.log").read_text(encoding="utf-8")
    assert "POST /importar" in log and "falha de teste" in log


# ---------------------------------------------------------------------------
# Nota avulsa com tomador digitado na hora
# ---------------------------------------------------------------------------
NOVO_TOMADOR = {
    "tipo_tomador": "novo",
    "documento": "",
    "novo_documento": "11.444.777/0001-61",
    "novo_razao_social": "Tomador Avulso LTDA",
    "novo_email": "",
    "servico_ctribnac": "01.01.01",
    "servico_ctribmun": "",
}


def test_nota_avulsa_aceita_tomador_digitado_e_codigo_proprio(cliente, dados_app):
    resposta = _avulsa(cliente, **NOVO_TOMADOR)
    assert resposta.status_code == 200, resposta.text

    cliente.get("/emitir/eventos")
    xml = next((dados_app / "notas" / "dry-run").glob("*.xml")).read_bytes()
    assert b"Tomador Avulso LTDA" in xml
    assert b"<cTribNac>010101</cTribNac>" in xml
    assert b"cTribMun" not in xml


def test_nota_avulsa_tomador_com_cnpj_invalido(cliente):
    resposta = _avulsa(cliente, **{**NOVO_TOMADOR, "novo_documento": "11444777000100"})
    assert resposta.status_code == 400
    assert "Confira o tomador" in resposta.json()["mensagem"]


def test_nota_avulsa_codigo_de_servico_invalido(cliente):
    resposta = _avulsa(cliente, **{**NOVO_TOMADOR, "servico_ctribnac": "1703"})
    assert resposta.status_code == 400
    assert "6 dígitos" in resposta.json()["mensagem"]


def test_nota_avulsa_salva_tomador_so_na_emissao_e_sem_sobrescrever(cliente, monkeypatch):
    from app.rotas_clientes import repositorio_clientes

    monkeypatch.setattr(ESTADO.trabalho, "iniciar", lambda **kw: None)
    dados = {**NOVO_TOMADOR, "novo_salvar": "1"}

    _avulsa(cliente, **dados)                      # simulação não salva
    assert repositorio_clientes().buscar("11444777000161") is None

    assert _avulsa(cliente, **dados, modo="emitir").status_code == 200
    assert repositorio_clientes().buscar("11444777000161").razao_social == "Tomador Avulso LTDA"

    _avulsa(cliente, **{**dados, "novo_razao_social": "Outro Nome"}, modo="emitir")
    assert repositorio_clientes().buscar("11444777000161").razao_social == "Tomador Avulso LTDA"


def test_tela_avulsa_mostra_formulario_sem_clientes(cliente):
    html = cliente.get("/avulsa").text
    assert 'id="novo_documento"' in html
    assert 'id="servico_ctribnac"' in html


# ---------------------------------------------------------------------------
# Erros amigáveis e ajuda
# ---------------------------------------------------------------------------
def test_pagina_inexistente_mostra_tela_amigavel(cliente):
    resposta = cliente.get("/nao-existe")
    assert resposta.status_code == 404
    assert "Página não encontrada" in resposta.text


def _cliente_que_quebra(dados_app, monkeypatch):
    import app.rotas_clientes as rotas

    def quebrar(*a, **k):
        raise RuntimeError("detalhe técnico interno")

    monkeypatch.setattr(rotas, "repositorio_clientes", quebrar)
    return TestClient(criar_app(Guardiao(TOKEN)), base_url="http://127.0.0.1:8765",
                      raise_server_exceptions=False)


def test_erro_inesperado_vira_tela_amigavel_e_log(dados_app, monkeypatch):
    navegador = _cliente_que_quebra(dados_app, monkeypatch)
    resposta = navegador.get("/clientes")
    assert resposta.status_code == 500
    assert "Algo não saiu como esperado" in resposta.text
    assert "detalhe técnico interno" not in resposta.text
    assert "detalhe técnico interno" in (ac.diretorio_dados() / "erros.log").read_text(encoding="utf-8")


def test_erro_inesperado_em_chamada_da_tela_vira_json(dados_app, monkeypatch):
    navegador = _cliente_que_quebra(dados_app, monkeypatch)
    resposta = navegador.post("/avulsa/emitir", headers={NOME_HEADER: TOKEN},
                              data={"documento": "11222333000181"})
    assert resposta.status_code == 500
    assert resposta.json()["ok"] is False
    assert "Algo não saiu como esperado" in resposta.json()["mensagem"]


def test_toda_ajuda_aponta_para_um_campo_que_existe():
    """Pega erro de digitação no id: ajuda que não aparece em lugar nenhum."""
    import re

    from app.ajuda import AJUDA

    pasta = Path(__file__).resolve().parent.parent / "app" / "templates"
    ids = set()
    for modelo in pasta.glob("*.html"):
        tela = re.search(r"pagina_atual = '(\w+)'", modelo.read_text(encoding="utf-8"))
        for id_ in re.findall(r'<label for="([^"]+)"', modelo.read_text(encoding="utf-8")):
            ids.add(id_)
            if tela:
                ids.add(f"{tela.group(1)}:{id_}")
    assert sorted(set(AJUDA) - ids) == []


def test_paginas_levam_os_textos_de_ajuda(cliente):
    html = cliente.get("/configuracao").text
    assert "window.EMISSOR_AJUDA" in html
    assert "senha de app" in html


def test_nota_avulsa_autorizada_entra_no_historico(cliente, monkeypatch):
    """O botão PDF (imprimir) fica no Histórico: a nota avulsa precisa estar lá."""
    from app.rotas_clientes import repositorio_emissoes

    monkeypatch.setattr("app.rotas_clientes.montar_emissor",
                        lambda *a, **k: _emissor_falso(ac.carregar(), "CHAVE-AVULSA1"))
    resposta = _avulsa(cliente, **NOVO_TOMADOR, modo="emitir")
    assert resposta.status_code == 200, resposta.text
    cliente.get("/emitir/eventos")

    assert "CHAVE-AVULSA1" in [n["chave_acesso"] if isinstance(n, dict) else n.chave_acesso
                              for n in repositorio_emissoes().listar()]


# ---------------------------------------------------------------------------
# Recibos (não vão à Receita)
# ---------------------------------------------------------------------------
def _recibo(cliente, **campos):
    dados = {**NOVO_TOMADOR, "novo_email": "cliente@alfa.com.br", "data": "2026-10-07",
             "valor": "4.500,00", "descricao": "consultoria prestada em setembro"}
    dados.update(campos)
    return cliente.post("/recibos/gerar", headers={NOME_HEADER: TOKEN}, data=dados)


def _com_nome_da_empresa():
    config = ac.carregar()
    config.prestador_razao_social = "Empresa Teste LTDA"
    ac.salvar(config)


def test_tela_recibos_abre(cliente):
    html = cliente.get("/recibos").text
    assert "Não vai para a Receita" in html
    assert 'id="novo_documento"' in html


def test_recibo_exige_nome_da_empresa(cliente):
    resposta = _recibo(cliente)
    assert resposta.status_code == 400
    assert "nome da empresa" in resposta.json()["mensagem"]


def test_recibo_gera_pdf_com_numeracao_propria(cliente, dados_app):
    _com_nome_da_empresa()
    primeiro = _recibo(cliente).json()
    segundo = _recibo(cliente).json()
    assert (primeiro["ok"], primeiro["numero"]) == (True, 1)
    assert segundo["numero"] == 2

    pdf = cliente.get(primeiro["pdf"])
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"
    assert list((dados_app / "notas" / "recibos" / "2026").glob("Recibo 1-0001 - *.pdf"))


def test_recibo_enviar_sem_email_configurado_explica(cliente):
    _com_nome_da_empresa()
    numero = _recibo(cliente).json()["numero"]
    resposta = cliente.post(f"/recibos/{numero}/enviar", headers={NOME_HEADER: TOKEN})
    assert resposta.status_code == 400
    assert "Configuração" in resposta.json()["mensagem"]


def test_recibo_enviar_por_email(cliente, monkeypatch):
    _com_nome_da_empresa()
    numero = _recibo(cliente).json()["numero"]
    enviados = []
    monkeypatch.setattr(
        "app.rotas_recibos.enviar_documento",
        lambda cfg, destino, assunto, corpo, anexo: enviados.append((destino, anexo)) or
        f"e-mail enviado para {destino}",
    )
    monkeypatch.setattr(
        ac.ConfiguracaoApp, "para_configuracao_email",
        lambda self, senha_smtp=None: type("C", (), {
            "servidor": "smtp", "usuario": "u", "senha": "s", "remetente_email": "r@x"})(),
    )
    resposta = cliente.post(f"/recibos/{numero}/enviar", headers={NOME_HEADER: TOKEN})
    assert resposta.json()["ok"] is True
    assert enviados[0][0] == "cliente@alfa.com.br"
    assert "enviado em" in cliente.get("/recibos").text


def test_tela_abre_pdf_de_nota_renomeada(cliente, dados_app, config, linha):
    from app.servidor import organizar_pasta_de_notas
    from tests.test_nomes_arquivos import _nota_antiga
    from tests.conftest import CHAVE_EXEMPLO

    pasta = dados_app / "notas" / "2026" / "09-setembro"
    _nota_antiga(pasta, config, linha)
    (pasta / f"{CHAVE_EXEMPLO}_11222333000181_danfse.pdf").unlink()   # força gerar
    assert organizar_pasta_de_notas() == 1

    resposta = cliente.get(f"/danfse/{CHAVE_EXEMPLO}")
    assert resposta.status_code == 200
    assert resposta.content[:4] == b"%PDF"
    assert "NFS-e 84" in resposta.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Ficha do cliente, menu e formatos
# ---------------------------------------------------------------------------
def test_ficha_do_cliente_tem_abas_com_notas_e_recibos(cliente):
    from app.rotas_clientes import repositorio_emissoes

    _cadastrar(cliente)
    repositorio_emissoes().registrar({
        "chave_acesso": "CHAVE-FICHA", "documento_tomador": "11.222.333/0001-81",
        "tomador": "Cliente Alfa", "valor_servico": "4500.00", "n_nfse": "84",
        "competencia": "2026-09", "emitida_em": "2026-10-07T10:00:00", "ambiente": "producao",
    })
    _com_nome_da_empresa()
    _recibo(cliente, tipo_tomador="cadastro", documento="11222333000181")

    html = cliente.get("/clientes/editar/11222333000181").text
    for aba in ("dados", "endereco", "mensal", "notas", "recibos"):
        assert f'data-aba="{aba}"' in html
    assert "11.222.333/0001-81" in html
    assert "09/2026" in html and "R$ 4.500,00" in html
    assert "/danfse/CHAVE-FICHA" in html
    assert "/recibos/1/pdf" in html
    assert "/avulsa?documento=11222333000181" in html


def test_cliente_novo_so_tem_as_abas_de_cadastro(cliente):
    html = cliente.get("/clientes/novo").text
    assert 'data-aba="dados"' in html
    assert 'data-aba="notas"' not in html


def test_avulsa_ja_vem_com_o_cliente_escolhido(cliente):
    _cadastrar(cliente)
    html = cliente.get("/avulsa?documento=11222333000181").text
    assert 'value="11222333000181"' in html
    assert "selected" in html.split('value="11222333000181"')[1].split(">")[0]


def test_menu_enxuto_e_aba_emitir(cliente):
    html = cliente.get("/").text
    menu = html.split("<nav>")[1].split("</nav>")[0]
    for item in ("Início", "Emitir nota", "Recibos", "Clientes", "Notas emitidas", "Configuração"):
        assert item in menu
    assert "Histórico" not in menu and "Recorrências" not in menu
    for tela in ("/avulsa", "/importar", "/emitir", "/recorrencias"):
        pagina = cliente.get(tela).text
        assert 'aria-label="Formas de emitir"' in pagina
        assert 'class="ativo">Emitir nota' in pagina.replace("\n", "").replace("  ", "")


def test_filtros_de_formato_brasileiro():
    from app.servidor import _competencia_br, _data_br
    from nfse.recibo import formatar_documento

    assert formatar_documento("11222333000181") == "11.222.333/0001-81"
    assert formatar_documento("52998224725") == "529.982.247-25"
    assert _data_br("2026-10-07T14:03:00") == "07/10/2026"
    assert _data_br("2026-10-07T14:03:00", True) == "07/10/2026 14:03"
    assert _competencia_br("2026-09") == "09/2026"
    assert _data_br("") == "—"


# ---------------------------------------------------------------------------
# Início, Configuração em abas, filtros de clientes, resultado amigável
# ---------------------------------------------------------------------------
def test_inicio_tem_atalhos_e_resumo_do_mes(cliente):
    from datetime import date

    from app.rotas_clientes import repositorio_emissoes

    repositorio_emissoes().registrar({
        "chave_acesso": "CHAVE-MES", "documento_tomador": "11222333000181", "tomador": "Cliente Alfa",
        "valor_servico": "1500.00", "n_nfse": "90", "competencia": date.today().strftime("%Y-%m"),
        "emitida_em": date.today().isoformat() + "T09:00:00", "ambiente": "homologacao",
    })
    html = cliente.get("/").text
    for atalho in ('href="/avulsa"', 'href="/recibos"', 'href="/emitir"', 'href="/clientes/novo"'):
        assert 'class="atalho' in html and atalho in html
    assert "R$ 1.500,00" in html
    assert "/danfse/CHAVE-MES" in html


def test_configuracao_em_abas(cliente):
    html = cliente.get("/configuracao").text
    for aba in ("empresa", "certificado", "servico", "email", "avancado"):
        assert f'data-aba="{aba}"' in html and f'data-painel="{aba}"' in html
    assert html.count('type="submit">Salvar configuração') == 1


def test_lista_de_clientes_em_abas(cliente):
    _cadastrar(cliente)
    _cadastrar(cliente, documento="11444777000161", razao_social="Cliente Beta", ativo="")
    _cadastrar(cliente, documento="52998224725", razao_social="Ana Mensal",
               dia_emissao_recorrente="5", descricao_recorrente="Consultoria")

    ativos = cliente.get("/clientes").text
    assert "Cliente Alfa" in ativos and "Ana Mensal" in ativos and "Cliente Beta" not in ativos
    assert "Cliente Beta" in cliente.get("/clientes?filtro=inativos").text
    mensais = cliente.get("/clientes?filtro=mensais").text
    assert "Ana Mensal" in mensais and "Cliente Alfa" not in mensais
    # Busca com o CNPJ pontuado acha o cliente gravado só com dígitos.
    assert "Cliente Alfa" in cliente.get("/clientes?busca=11.222.333/0001-81").text


def test_evento_de_nota_traz_o_numero_da_nfse():
    from app.sessao import _evento_para_json
    from nfse.logs import RegistroLinha
    from nfse.servico import EventoProgresso

    registro = RegistroLinha(linha_planilha=1, situacao="AUTORIZADA", n_nfse="84",
                             documento_tomador="11222333000181")
    dados = _evento_para_json(EventoProgresso(tipo="fim_linha", mensagem="", situacao="AUTORIZADA",
                                                  registro=registro))
    assert dados["registro"]["n_nfse"] == "84"
    assert dados["registro"]["documento_tomador"] == "11222333000181"


# ---------------------------------------------------------------------------
# Compartilhamento entre computadores pela tela
# ---------------------------------------------------------------------------
def test_ligar_compartilhamento_pela_configuracao(cliente, dados_app):
    from nfse import sincronizacao

    pasta = dados_app / "Drive" / "Emissor"
    pasta.mkdir(parents=True)
    try:
        resposta = cliente.post("/configuracao", headers={NOME_HEADER: TOKEN}, data={
            "ambiente": "homologacao", "prestador_cnpj": "11222333000181", "serie_dps": "1",
            "pasta_compartilhada": str(pasta), "mensais_neste_computador": "on",
        }, follow_redirects=False)
        assert resposta.status_code == 303
        assert sincronizacao.ativo() is not None

        _cadastrar(cliente)
        resultado = cliente.post("/sincronizacao/agora", headers={NOME_HEADER: TOKEN}).json()
        assert resultado["ok"] is True
        assert (pasta / "computadores" / "serie-1" / "eventos.jsonl").read_text(encoding="utf-8")
        assert (pasta / "computadores" / "serie-1" / "dados-backup.db").is_file()

        html = cliente.get("/configuracao").text
        assert "Compartilhamento ligado" in html
        assert "Computador da série 1" in html          # backup disponível para restaurar
    finally:
        sincronizacao.desativar()


def test_pdf_de_nota_emitida_em_outro_computador(cliente, dados_app, config, linha):
    from app.rotas_clientes import repositorio_emissoes
    from nfse import sincronizacao
    from tests.conftest import CHAVE_EXEMPLO, montar_nfse_xml

    pasta = dados_app / "Drive" / "Emissor"
    copia = pasta / "notas" / "2026" / "09-setembro"
    copia.mkdir(parents=True)
    base = "NFS-e 84 - Cliente Alfa Tecnologia LTDA - 09-2026"
    (copia / f"{base}.xml").write_bytes(montar_nfse_xml(config, linha))
    (copia / f"{base} - dados.json").write_text(json.dumps({
        "chave_acesso": CHAVE_EXEMPLO, "arquivos": {"xml_nfse": "C:/outro/pc.xml"}}), encoding="utf-8")
    configuracao = ac.carregar()
    configuracao.pasta_compartilhada = str(pasta)
    ac.salvar(configuracao)
    # A nota chegou pela sincronização: está no histórico, mas não na pasta local.
    repositorio_emissoes().registrar({"chave_acesso": CHAVE_EXEMPLO, "tomador": "Cliente Alfa"},
                                     publicar=False)
    try:
        resposta = cliente.get(f"/danfse/{CHAVE_EXEMPLO}")
        assert resposta.status_code == 200
        assert resposta.content[:4] == b"%PDF"
    finally:
        sincronizacao.desativar()


# ---------------------------------------------------------------------------
# Versão nova nunca roda com o JS/CSS antigo do navegador
# ---------------------------------------------------------------------------
def test_estaticos_com_versao_e_sem_cache_velho(cliente):
    import re

    html = cliente.get("/").text
    versao = re.search(r'/static/app\.js\?v=([0-9a-f]{10})"', html)
    assert versao, "app.js precisa sair com a versão na URL"
    assert f"/static/estilo.css?v={versao.group(1)}" in html
    resposta = cliente.get(f"/static/app.js?v={versao.group(1)}")
    assert resposta.status_code == 200
    assert resposta.headers["cache-control"] == "no-cache"


def test_recibo_sem_nome_da_empresa_desabilita_o_botao(cliente):
    html = cliente.get("/recibos").text
    botao = html.split('id="btn-gerar-recibo"')[1].split(">")[0]
    assert "disabled" in botao
    _com_nome_da_empresa()
    botao = cliente.get("/recibos").text.split('id="btn-gerar-recibo"')[1].split(">")[0]
    assert "disabled" not in botao
