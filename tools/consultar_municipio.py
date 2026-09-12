"""
Pergunta à Sefin Nacional o que o município aceita, antes de tentar emitir.

Existe por causa de uma rejeição real:

    [E0312] O código de tributação nacional informado não está administrado
    pelo município de incidência do ISSQN na data de competência informada
    na DPS, conforme a lista de serviços nacional do Sistema Nacional NFS-e.

Essa recusa não é defeito do arquivo: é o município dizendo que não cuida
daquele código de serviço naquela competência — ou que nem está conveniado ao
Sistema Nacional. Descobrir isso tentando emitir é caro, e a mensagem não diz
qual código usar no lugar. Este utilitário consulta os parâmetros municipais
direto na Sefin, usando o mesmo certificado da emissão.

Uso (com o ambiente virtual ativado, na pasta do projeto):

    python tools/consultar_municipio.py
    python tools/consultar_municipio.py --municipio 3304557 --servico 170101 --competencia 2026-09

Sem argumentos, usa o que está na Configuração do aplicativo.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nfse import armazenamento_config as ac  # noqa: E402
from nfse.certificado import (  # noqa: E402
    ErroCertificado,
    carregar_certificado,
    criar_sessao_mtls,
)

# Rotas de parâmetros municipais do contrato REST da Sefin Nacional. Não são
# usadas na emissão, por isso moram aqui e não em `nfse/config.py`.
ROTAS = {
    "Convênio do município com o Sistema Nacional": "/parametros_municipais/{municipio}/convenio",
    "Alíquotas e regimes do serviço": "/parametros_municipais/{municipio}/{servico}",
}


def consultar(sessao, base: str, rota: str, timeout: int) -> tuple[int, object]:
    resposta = sessao.get(base.rstrip("/") + rota, timeout=timeout)
    try:
        return resposta.status_code, resposta.json()
    except ValueError:
        return resposta.status_code, resposta.text[:400]


def bases_candidatas(config) -> list[str]:
    """Endereços onde a API de parâmetros municipais pode estar.

    O módulo de parametrização é separado do de emissão e a documentação
    oficial não estava acessível para confirmar o host. Em vez de tentar um
    por rodada, o utilitário varre os candidatos e diz qual respondeu — são
    consultas de leitura, com o mesmo certificado da emissão.
    """
    dominio = "producaorestrita.nfse.gov.br" if config.ambiente == "homologacao" else "nfse.gov.br"
    candidatos = [
        config.url_base,
        f"https://sefin.{dominio}",
        f"https://adn.{dominio}/contribuintes",
        f"https://adn.{dominio}",
        f"https://parametros.{dominio}",
        f"https://parametrosmunicipais.{dominio}",
        f"https://www.{dominio}",
    ]
    vistos, unicos = set(), []
    for base in candidatos:
        if base.rstrip("/") not in vistos:
            vistos.add(base.rstrip("/"))
            unicos.append(base)
    return unicos


def descobrir_base(sessao, config, municipio: str) -> str | None:
    """Devolve a primeira base que responde à consulta de convênio."""
    sonda = ROTAS["Convênio do município com o Sistema Nacional"].format(
        municipio=municipio, servico="", competencia=""
    )
    for base in bases_candidatas(config):
        try:
            status, _ = consultar(sessao, base, sonda, config.timeout_segundos)
        except Exception as exc:  # noqa: BLE001 — host inexistente é resposta, não falha
            print(f"   {base} -> {type(exc).__name__}")
            continue
        print(f"   {base} -> HTTP {status}")
        if status < 400:
            return base
    return None


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--municipio", help="código IBGE de 7 dígitos")
    analisador.add_argument("--servico", help="código de tributação nacional (cTribNac)")
    analisador.add_argument("--competencia", help="AAAA-MM")
    analisador.add_argument(
        "--base",
        help="URL base da API de parametrização, se ela não for a mesma da emissão",
    )
    argumentos = analisador.parse_args()

    try:
        config, _ = ac.carregar_do_app()
    except ac.ErroConfiguracaoApp as exc:
        print(f"Configuração do aplicativo incompleta: {exc}")
        return 1

    municipio = argumentos.municipio or config.servico.codigo_municipio_prestacao
    servico = argumentos.servico or config.servico.codigo_tributacao_nacional
    competencia = argumentos.competencia or date.today().strftime("%Y-%m")

    print(f"Ambiente ...: {config.ambiente}")
    print(f"Município ..: {municipio}")
    print(f"Serviço ....: {servico}")
    print(f"Competência : {competencia}")
    print()

    try:
        certificado = carregar_certificado(config)
        certificado.validar_vigencia()
        sessao = criar_sessao_mtls(certificado)
    except ErroCertificado as exc:
        print(f"Certificado: {exc}")
        return 1

    if argumentos.base:
        base = argumentos.base
    else:
        print("Procurando a API de parâmetros municipais:")
        base = descobrir_base(sessao, config, municipio)
        print()
        if base is None:
            print(
                "Nenhum dos endereços conhecidos respondeu à consulta de convênio.\n"
                "Isso não diz nada sobre o município — só que não achei a API por aqui.\n"
                "Se você souber o endereço certo, passe em --base. A lista oficial de\n"
                "municípios conveniados também está no portal da NFS-e Nacional."
            )
            return 1

    print(f"API ........: {base}")
    print()

    nao_encontrado = False
    houve_falha = False
    for titulo, molde in ROTAS.items():
        rota = molde.format(municipio=municipio, servico=servico, competencia=competencia)
        try:
            status, corpo = consultar(sessao, base, rota, config.timeout_segundos)
        except Exception as exc:  # noqa: BLE001 — rede/TLS vira aviso, não traceback
            print(f"[FALHOU] {titulo}\n         {exc}\n")
            houve_falha = True
            continue

        marca = "OK" if status == 200 else f"HTTP {status}"
        print(f"[{marca}] {titulo}")
        print(f"         {rota}")
        # Página de erro do servidor web não diz nada de útil e ocupa a tela toda.
        if isinstance(corpo, str) and corpo.lstrip().lower().startswith(("<!doctype", "<html")):
            print("         (o servidor respondeu uma página de erro, não dados)")
        else:
            texto = json.dumps(corpo, ensure_ascii=False, indent=2) if isinstance(corpo, (dict, list)) else str(corpo)
            for linha in texto.splitlines():
                print(f"         {linha}")
        print()
        nao_encontrado = nao_encontrado or status == 404
        houve_falha = houve_falha or status >= 400

    if nao_encontrado:
        print(
            "HTTP 404 aqui significa que esta API não tem essa rota — e NÃO que o\n"
            "município esteja sem convênio. Os parâmetros municipais ficam num\n"
            "módulo à parte do de emissão; se você souber o endereço dele, passe em\n"
            "--base. A lista oficial de municípios conveniados também está no\n"
            "portal da NFS-e Nacional."
        )
    elif houve_falha:
        print(
            "Alguma consulta não respondeu 200. Se o município aparece sem convênio,\n"
            "ou o serviço não consta para ele, a emissão pela NFS-e Nacional vai\n"
            "continuar sendo recusada com E0312 — nesse caso o caminho é o sistema\n"
            "próprio da prefeitura, ou outro código de serviço."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
