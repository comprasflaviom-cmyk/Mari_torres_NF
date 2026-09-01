"""
Configuração central do emissor.

>>> ONDE COLOCAR SUAS CREDENCIAIS <<<
Nada aqui é lido de hardcode: tudo vem de variáveis de ambiente (arquivo `.env`).
Copie `.env.exemplo` para `.env` e preencha. O `.env` está no .gitignore.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RAIZ = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# ENDPOINTS OFICIAIS (Sefin Nacional / ADN)
# ---------------------------------------------------------------------------
# A emissão é feita no ambiente da Sefin Nacional. Municípios conveniados —
# caso do Rio de Janeiro (IBGE 3304557) — usam este mesmo endpoint federal.
#
# Alterne entre os dois pela variável AMBIENTE no .env:
#   AMBIENTE=homologacao  -> produção restrita (use para TODOS os seus testes)
#   AMBIENTE=producao     -> emite nota com valor fiscal real
ENDPOINTS = {
    # tpAmb = 2 (homologação / "produção restrita")
    "homologacao": {
        "sefin": "https://sefin.producaorestrita.nfse.gov.br/SefinNacional",
        "tp_amb": 2,
    },
    # tpAmb = 1 (produção)
    "producao": {
        "sefin": "https://sefin.nfse.gov.br/sefinnacional",
        "tp_amb": 1,
    },
}

# Rotas do contrato REST da Sefin Nacional
ROTA_EMISSAO = "/nfse"                     # POST  -> envia a DPS
ROTA_CONSULTA_CHAVE = "/nfse/{chave}"      # GET   -> consulta NFS-e pela chave
ROTA_DANFSE = "/danfse/{chave}"            # GET   -> PDF (DANFSe) da nota

VERSAO_APLICATIVO = "1.00"   # verAplic — identificação do seu emissor
VERSAO_LAYOUT = "1.00"       # atributo versao do elemento <DPS>
NAMESPACE_DPS = "http://www.sped.fazenda.gov.br/nfse"


@dataclass(frozen=True)
class Prestador:
    """Dados da SUA empresa (o emitente da nota)."""

    cnpj: str                  # somente dígitos, 14 posições
    inscricao_municipal: str   # CCM da prefeitura do Rio; vazio se não houver
    codigo_municipio: str      # código IBGE de 7 dígitos do município de emissão

    # Regime tributário — confira com o seu contador antes de emitir em produção.
    # opSimpNac: 1=Não optante | 2=Optante MEI | 3=Optante Simples Nacional (ME/EPP)
    opcao_simples_nacional: int = 3
    # regEspTrib: 0=Nenhum | 1=Ato Coop. | 2=Estimativa | 3=Soc. Profissionais
    #             4=Cooperativa | 5=MEI | 6=ME/EPP Simples
    regime_especial: int = 0


@dataclass(frozen=True)
class ParametrosServico:
    """Parâmetros fiscais fixos do serviço de consultoria."""

    # cTribNac — Código de Tributação Nacional (6 dígitos), da tabela de
    # Classificação de Serviços Nacional derivada da LC 116/2003.
    # 170101 = subitem 17.01 "Assessoria ou consultoria de qualquer natureza".
    # >>> CONFIRA o desdobro correto para a sua atividade em
    #     https://www.nfse.gov.br/consultapublica/ (Consulta de Serviços)
    codigo_tributacao_nacional: str = "170101"

    # cLocPrestacao — município onde o serviço é prestado (IBGE).
    # Para consultoria, em regra é o município do prestador (art. 3º da LC 116),
    # com exceções que o seu contador deve validar.
    codigo_municipio_prestacao: str = "3304557"  # Rio de Janeiro/RJ

    # tribISSQN: 1=Operação tributável | 2=Exportação | 3=Não incidência | 4=Imunidade
    tributacao_issqn: int = 1
    # tpRetISSQN: 1=Não retido | 2=Retido pelo tomador | 3=Retido pelo intermediário
    tipo_retencao_issqn: int = 1
    # indTotTrib: 0=Não informa totais aproximados | 1=Informa valores | 2="Sem valor"
    indicador_total_tributos: int = 0

    # Alíquota do ISS, em percentual (ex.: Decimal("2.00") = 2%).
    # Optantes do Simples Nacional normalmente NÃO informam alíquota na DPS —
    # deixe None nesse caso. Consulte o contador.
    aliquota_iss: Decimal | None = None


@dataclass
class Configuracao:
    ambiente: str
    prestador: Prestador
    servico: ParametrosServico

    # Certificado Digital A1 — veja o README, seção "Convertendo o certificado".
    caminho_pfx: Path | None          # .pfx/.p12 original (caminho preferido)
    senha_pfx: str | None
    caminho_certificado: Path | None  # certificado.crt (PEM), alternativa ao .pfx
    caminho_chave: Path | None        # chave.key (PEM), alternativa ao .pfx
    senha_chave: str | None           # senha da chave PEM, se ela estiver cifrada

    caminho_planilha: Path
    diretorio_notas: Path
    diretorio_logs: Path

    serie_dps: str
    numero_dps_inicial: int
    timeout_segundos: int
    max_tentativas: int

    # Pasta de espelhamento (OneDrive, Drive, rede). None = sem backup.
    # Fica por último com padrão para não obrigar quem já constrói Configuracao.
    diretorio_backup: Path | None = None

    # Campos derivados
    url_base: str = field(init=False)
    tp_amb: int = field(init=False)

    def __post_init__(self) -> None:
        if self.ambiente not in ENDPOINTS:
            raise ValueError(
                f"AMBIENTE inválido: {self.ambiente!r}. Use 'homologacao' ou 'producao'."
            )
        self.url_base = ENDPOINTS[self.ambiente]["sefin"]
        self.tp_amb = ENDPOINTS[self.ambiente]["tp_amb"]

    @property
    def usa_pfx(self) -> bool:
        return self.caminho_pfx is not None


def _caminho_opcional(valor: str | None) -> Path | None:
    if not valor:
        return None
    caminho = Path(valor)
    return caminho if caminho.is_absolute() else RAIZ / caminho


def carregar_configuracao() -> Configuracao:
    """Lê o `.env` e devolve a configuração validada."""
    aliquota = os.getenv("ISS_ALIQUOTA", "").strip()

    config = Configuracao(
        ambiente=os.getenv("AMBIENTE", "homologacao").strip().lower(),
        prestador=Prestador(
            cnpj=os.getenv("PRESTADOR_CNPJ", "").strip(),
            inscricao_municipal=os.getenv("PRESTADOR_IM", "").strip(),
            codigo_municipio=os.getenv("PRESTADOR_COD_MUNICIPIO", "3304557").strip(),
            opcao_simples_nacional=int(os.getenv("PRESTADOR_SIMPLES_NACIONAL", "3")),
            regime_especial=int(os.getenv("PRESTADOR_REGIME_ESPECIAL", "0")),
        ),
        servico=ParametrosServico(
            codigo_tributacao_nacional=os.getenv("SERVICO_CTRIBNAC", "170101").strip(),
            codigo_municipio_prestacao=os.getenv(
                "SERVICO_COD_MUNICIPIO", "3304557"
            ).strip(),
            tributacao_issqn=int(os.getenv("SERVICO_TRIB_ISSQN", "1")),
            tipo_retencao_issqn=int(os.getenv("SERVICO_RET_ISSQN", "1")),
            indicador_total_tributos=int(os.getenv("SERVICO_IND_TOT_TRIB", "0")),
            aliquota_iss=Decimal(aliquota) if aliquota else None,
        ),
        caminho_pfx=_caminho_opcional(os.getenv("CERT_PFX")),
        senha_pfx=os.getenv("CERT_PFX_SENHA") or None,
        caminho_certificado=_caminho_opcional(os.getenv("CERT_CRT", "certificado.crt")),
        caminho_chave=_caminho_opcional(os.getenv("CERT_KEY", "chave.key")),
        senha_chave=os.getenv("CERT_KEY_SENHA") or None,
        caminho_planilha=_caminho_opcional(os.getenv("PLANILHA", "faturamento.xlsx")),
        diretorio_notas=_caminho_opcional(os.getenv("DIR_NOTAS", "notas")),
        diretorio_logs=_caminho_opcional(os.getenv("DIR_LOGS", "logs")),
        diretorio_backup=_caminho_opcional(os.getenv("DIR_BACKUP")),
        serie_dps=os.getenv("DPS_SERIE", "1").strip(),
        numero_dps_inicial=int(os.getenv("DPS_NUMERO_INICIAL", "1")),
        timeout_segundos=int(os.getenv("HTTP_TIMEOUT", "60")),
        max_tentativas=int(os.getenv("HTTP_MAX_TENTATIVAS", "3")),
    )

    if not config.prestador.cnpj.isdigit() or len(config.prestador.cnpj) != 14:
        raise ValueError("PRESTADOR_CNPJ deve conter exatamente 14 dígitos.")
    return config
