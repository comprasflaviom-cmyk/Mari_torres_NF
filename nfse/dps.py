"""
Montagem da DPS (Declaração de Prestação de Serviço).

LEIA ANTES DE USAR
------------------
A API da NFS-e Nacional **não recebe a DPS como JSON**. O contrato é:

    POST {url_base}/nfse
    {"dpsXmlGZipB64": "<base64( gzip( XML da DPS assinado em XMLDSig ) )>"}

Ou seja, o JSON é apenas o envelope. Por isso este módulo faz duas coisas:

1. `montar_dps()` devolve um **dicionário ordenado** com todos os campos — é a
   sua "visão JSON" da DPS, fácil de inspecionar, logar e testar;
2. `dps_para_xml()` serializa esse dicionário no XML do layout, na ordem exata
   exigida pelo `sequence` do schema.

>>> VALIDE o XML gerado contra o `DPS_v1.00.xsd` oficial (baixe o pacote de
    schemas em https://www.nfse.gov.br/ , área de documentação técnica) antes de
    ir para produção. A ordem e a obrigatoriedade dos campos vêm do XSD.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

from .config import Configuracao, NAMESPACE_DPS, VERSAO_APLICATIVO, VERSAO_LAYOUT
from .planilha import LinhaFaturamento

FUSO_BRASILIA = timezone(timedelta(hours=-3))


def gerar_id_dps(config: Configuracao, numero_dps: int) -> str:
    """Monta o `Id` de 45 posições do elemento `infDPS`.

    Formato: "DPS" + cLocEmi(7) + tpInsc(1) + inscrição(14) + série(5) + nDPS(15)
    tpInsc: 1 = CPF, 2 = CNPJ.
    """
    identificador = (
        "DPS"
        + config.prestador.codigo_municipio.zfill(7)
        + "2"
        + config.prestador.cnpj.zfill(14)
        + config.serie_dps.zfill(5)
        + str(numero_dps).zfill(15)
    )
    assert len(identificador) == 45, f"Id da DPS com tamanho inválido: {len(identificador)}"
    return identificador


def _valor(v: Decimal) -> str:
    """Valores monetários vão com ponto decimal e 2 casas: 1234.56"""
    return f"{v.quantize(Decimal('0.01')):f}"


def montar_dps(
    config: Configuracao,
    linha: LinhaFaturamento,
    numero_dps: int,
    competencia: date | None = None,
    emitido_em: datetime | None = None,
) -> dict[str, Any]:
    """Monta a DPS de UMA linha da planilha, como dicionário ordenado.

    `competencia` é o mês de referência do serviço (dCompet). Por padrão usa a
    data de emissão. Se você fatura em setembro um serviço prestado em agosto,
    passe `date(2026, 8, 1)` aqui.
    """
    emitido_em = emitido_em or datetime.now(FUSO_BRASILIA)
    competencia = competencia or emitido_em.date()
    prest = config.prestador
    serv = config.servico

    # ---- Prestador (sua empresa) -----------------------------------------
    bloco_prestador: dict[str, Any] = {"CNPJ": prest.cnpj}
    if prest.inscricao_municipal:
        bloco_prestador["IM"] = prest.inscricao_municipal
    bloco_prestador["regTrib"] = {
        "opSimpNac": prest.opcao_simples_nacional,
        "regEspTrib": prest.regime_especial,
    }

    # ---- Tomador (seu cliente) -------------------------------------------
    # A ordem das chaves reproduz o `sequence` do XSD: identificação, nome,
    # endereço, telefone, e-mail.
    bloco_tomador: dict[str, Any] = {
        linha.tipo_documento: linha.documento_tomador,  # <CNPJ> ou <CPF>
        "xNome": linha.razao_social,
    }

    endereco = _montar_endereco(linha)
    if endereco:
        bloco_tomador["end"] = endereco
    if linha.extras.get("Telefone"):
        bloco_tomador["fone"] = "".join(c for c in linha.extras["Telefone"] if c.isdigit())
    if linha.email:
        bloco_tomador["email"] = linha.email

    # ---- Serviço ----------------------------------------------------------
    bloco_servico = {
        "locPrest": {"cLocPrestacao": serv.codigo_municipio_prestacao},
        "cServ": {
            "cTribNac": serv.codigo_tributacao_nacional,
            "xDescServ": linha.descricao,
        },
    }

    # ---- Valores e tributação --------------------------------------------
    tributacao_municipal: dict[str, Any] = {"tribISSQN": serv.tributacao_issqn}
    if serv.aliquota_iss is not None:
        # Optantes do Simples Nacional normalmente NÃO informam pAliq —
        # deixe ISS_ALIQUOTA vazio no .env nesse caso.
        tributacao_municipal["pAliq"] = f"{serv.aliquota_iss.quantize(Decimal('0.01')):f}"
    tributacao_municipal["tpRetISSQN"] = serv.tipo_retencao_issqn

    bloco_valores = {
        "vServPrest": {"vServ": _valor(linha.valor_servico)},
        "trib": {
            "tribMun": tributacao_municipal,
            "totTrib": {"indTotTrib": serv.indicador_total_tributos},
        },
    }

    # ---- DPS completa -----------------------------------------------------
    return {
        "@versao": VERSAO_LAYOUT,
        "infDPS": {
            "@Id": gerar_id_dps(config, numero_dps),
            "tpAmb": config.tp_amb,          # 1=produção | 2=homologação
            "dhEmi": emitido_em.replace(microsecond=0).isoformat(),
            "verAplic": VERSAO_APLICATIVO,
            "serie": config.serie_dps,
            "nDPS": str(numero_dps),
            "dCompet": competencia.isoformat(),
            "tpEmit": 1,                     # 1 = emitido pelo próprio prestador
            "cLocEmi": config.prestador.codigo_municipio,
            "prest": bloco_prestador,
            "toma": bloco_tomador,
            "serv": bloco_servico,
            "valores": bloco_valores,
        },
    }


def _montar_endereco(linha: LinhaFaturamento) -> dict[str, Any] | None:
    """Endereço do tomador. Só é montado se houver município informado.

    O layout aceita tomador sem endereço completo; se a sua prefeitura recusar,
    preencha as colunas opcionais na planilha (veja `planilha.COLUNAS_OPCIONAIS`).
    """
    extras = linha.extras
    municipio = "".join(c for c in extras.get("Cod_Municipio", "") if c.isdigit())
    if not municipio:
        return None

    endereco_nacional: dict[str, Any] = {"cMun": municipio.zfill(7)}
    cep = "".join(c for c in extras.get("CEP", "") if c.isdigit())
    if cep:
        endereco_nacional["CEP"] = cep.zfill(8)

    endereco: dict[str, Any] = {"endNac": endereco_nacional}
    for chave_planilha, tag in (
        ("Logradouro", "xLgr"),
        ("Numero", "nro"),
        ("Complemento", "xCpl"),
        ("Bairro", "xBairro"),
    ):
        if extras.get(chave_planilha):
            endereco[tag] = extras[chave_planilha]
    return endereco


# ---------------------------------------------------------------------------
# Serialização dicionário -> XML
# ---------------------------------------------------------------------------
def dps_para_xml(dps: dict[str, Any]) -> bytes:
    """Converte o dicionário da DPS no XML do layout (UTF-8, sem quebras).

    Chaves iniciadas por "@" viram atributos; as demais, elementos, na ordem
    de inserção do dicionário (que reproduz o `sequence` do XSD).
    """
    ET.register_namespace("", NAMESPACE_DPS)
    raiz = ET.Element(f"{{{NAMESPACE_DPS}}}DPS")
    _preencher(raiz, dps)
    # A assinatura XMLDSig exige XML sem indentação nem quebras extras.
    return ET.tostring(raiz, encoding="utf-8", xml_declaration=True)


def _preencher(elemento: ET.Element, dados: dict[str, Any]) -> None:
    for chave, valor in dados.items():
        if chave.startswith("@"):
            elemento.set(chave[1:], str(valor))
        elif isinstance(valor, dict):
            filho = ET.SubElement(elemento, f"{{{NAMESPACE_DPS}}}{chave}")
            _preencher(filho, valor)
        else:
            filho = ET.SubElement(elemento, f"{{{NAMESPACE_DPS}}}{chave}")
            filho.text = str(valor)
