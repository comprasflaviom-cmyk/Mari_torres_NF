"""Testes da interface web local — travas de segurança e fluxo de emissão."""

from __future__ import annotations

import datetime as dt
import io
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

from app.seguranca import NOME_HEADER, Guardiao
from app.servidor import criar_app
from app.sessao import ESTADO, TrabalhoEmissao
from nfse import armazenamento_config as ac
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


def test_planilha_de_cliente_nao_cadastrado_segue_normal(cliente):
    _importar(cliente)
    previa = ESTADO.importacao.validas[0]
    assert previa.cliente_ativo is None
    assert previa.do_cadastro == []


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
    assert "Selecione um cliente" in resposta.json()["mensagem"]


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
