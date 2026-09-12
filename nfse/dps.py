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

>>> VALIDE o XML gerado contra o `DPS_v1.01.xsd` oficial (baixe o pacote de
    schemas em https://www.nfse.gov.br/ , área de documentação técnica) antes de
    ir para produção. A ordem e a obrigatoriedade dos campos vêm do XSD.

    Atenção: o `TSSerieDPS` da versão 1.01 tem um `pattern` com `^...$` que,
    em XSD (diferente de regex Perl), são caracteres LITERAIS — o padrão
    rejeita qualquer valor numérico normal. É um bug conhecido do schema
    oficial (a versão 1.00 não tinha esse pattern), não do código aqui. Se o
    validador acusar isso em `<serie>`, é esperado — não há o que corrigir
    do nosso lado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

from .config import Configuracao, NAMESPACE_DPS, VERSAO_APLICATIVO, VERSAO_LAYOUT
from .planilha import LinhaFaturamento

FUSO_BRASILIA = timezone(timedelta(hours=-3))

# A Sefin recusa com `[E0008] A data de emissão da DPS não pode ser posterior à
# data do seu processamento` — e quem decide o `dhEmi` é o relógio desta
# máquina. Numa rejeição real o `dhEmi` saiu apenas 0,145 s antes do instante
# em que a Sefin processou: margem nenhuma para o relógio local estar um
# segundo adiantado. Estes segundos de folga resolvem isso sem distorcer nada
# — a data continua a mesma, e a folga é menor que qualquer arredondamento
# fiscal.
MARGEM_RELOGIO = timedelta(seconds=10)


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
    if emitido_em is None:
        agora = datetime.now(FUSO_BRASILIA)
        emitido_em = agora - MARGEM_RELOGIO
        # A competência sai do relógio sem a folga: perto da meia-noite do dia
        # 1º, descontar os segundos jogaria a nota para o mês anterior.
        competencia = competencia or agora.date()
    else:
        competencia = competencia or emitido_em.date()
    prest = config.prestador
    serv = config.servico

    # ---- Prestador (sua empresa) -----------------------------------------
    bloco_prestador: dict[str, Any] = {"CNPJ": prest.cnpj}
    if prest.inscricao_municipal:
        bloco_prestador["IM"] = prest.inscricao_municipal
    # A ordem reproduz o `sequence` do XSD: opSimpNac, regApTribSN, regEspTrib.
    # O regime de apuração só existe para optante ME/EPP — é o campo que a nota
    # real da empresa traz como "Regime de apuração dos tributos federais e
    # municipal pelo Simples Nacional", e que faltava aqui.
    bloco_prestador["regTrib"] = {"opSimpNac": prest.opcao_simples_nacional}
    if prest.opcao_simples_nacional == 3:
        bloco_prestador["regTrib"]["regApTribSN"] = prest.regime_apuracao_sn
    bloco_prestador["regTrib"]["regEspTrib"] = prest.regime_especial

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
    # Ordem conferida contra TCTribMunicipal do schema oficial v1.01: tribISSQN,
    # depois (todos opcionais) cPaisResult/tpImunidade/exigSusp/BM — que não
    # usamos aqui —, tpRetISSQN, e só por último pAliq. Colocar pAliq antes de
    # tpRetISSQN (como uma leitura "natural" sugeriria) gera XML inválido.
    tributacao_municipal: dict[str, Any] = {
        "tribISSQN": serv.tributacao_issqn,
        "tpRetISSQN": serv.tipo_retencao_issqn,
    }
    if serv.aliquota_iss is not None:
        # Optantes do Simples Nacional normalmente NÃO informam pAliq —
        # deixe ISS_ALIQUOTA vazio no .env nesse caso.
        tributacao_municipal["pAliq"] = f"{serv.aliquota_iss.quantize(Decimal('0.01')):f}"

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


# (coluna da planilha/cadastro, rótulo para mensagem de erro). Conferido
# contra o TCEndereco/TCEnderNac do schema oficial (esquemas XSD v1.01,
# tiposComplexos): dentro de `end`, só xCpl tem minOccurs="0" — todos os
# outros, inclusive CEP dentro de endNac, são obrigatórios em conjunto.
_CAMPOS_OBRIGATORIOS_ENDERECO = (
    ("CEP", "CEP"),
    ("Logradouro", "logradouro"),
    ("Numero", "número"),
    ("Bairro", "bairro"),
)


class ErroDPS(ValueError):
    """A linha não tem dado suficiente para montar uma DPS válida."""


def _montar_endereco(linha: LinhaFaturamento) -> dict[str, Any] | None:
    """Endereço do tomador. Só é montado se houver município informado.

    O schema não permite endereço "pela metade": se o município está
    presente, CEP, logradouro, número e bairro são todos obrigatórios juntos
    (só o complemento é opcional). Faltando algum, é melhor recusar aqui —
    com uma mensagem que diz exatamente o que falta — do que gerar um XML
    que o schema (ou a própria Sefin) vai rejeitar sem esse contexto.
    """
    extras = linha.extras
    municipio = "".join(c for c in extras.get("Cod_Municipio", "") if c.isdigit())
    if not municipio:
        return None  # sem município: o bloco inteiro é opcional, tudo bem omitir

    faltando = [
        rotulo for coluna, rotulo in _CAMPOS_OBRIGATORIOS_ENDERECO
        if not extras.get(coluna, "").strip()
    ]
    if faltando:
        raise ErroDPS(
            "Endereço do tomador incompleto: o schema da NFS-e Nacional exige "
            f"CEP, logradouro, número e bairro juntos quando há município. "
            f"Falta: {', '.join(faltando)}. Complete o cadastro do cliente ou "
            "as colunas da planilha antes de emitir."
        )

    cep = "".join(c for c in extras["CEP"] if c.isdigit())
    endereco: dict[str, Any] = {
        "endNac": {"cMun": municipio.zfill(7), "CEP": cep.zfill(8)},
        "xLgr": extras["Logradouro"],
        "nro": extras["Numero"],
    }
    if extras.get("Complemento"):
        endereco["xCpl"] = extras["Complemento"]
    endereco["xBairro"] = extras["Bairro"]
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
