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
    "Serviço na competência": "/parametros_municipais/{municipio}/{servico}/{competencia}",
    "Alíquota do serviço": "/parametros_municipais/{municipio}/{servico}/aliquota/{competencia}",
}


def consultar(sessao, base: str, rota: str, timeout: int) -> tuple[int, object]:
    resposta = sessao.get(base.rstrip("/") + rota, timeout=timeout)
    try:
        return resposta.status_code, resposta.json()
    except ValueError:
        return resposta.status_code, resposta.text[:400]


def main() -> int:
    analisador = argparse.ArgumentParser(description=__doc__)
    analisador.add_argument("--municipio", help="código IBGE de 7 dígitos")
    analisador.add_argument("--servico", help="código de tributação nacional (cTribNac)")
    analisador.add_argument("--competencia", help="AAAA-MM")
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

    houve_falha = False
    for titulo, molde in ROTAS.items():
        rota = molde.format(municipio=municipio, servico=servico, competencia=competencia)
        try:
            status, corpo = consultar(sessao, config.url_base, rota, config.timeout_segundos)
        except Exception as exc:  # noqa: BLE001 — rede/TLS vira aviso, não traceback
            print(f"[FALHOU] {titulo}\n         {exc}\n")
            houve_falha = True
            continue

        marca = "OK" if status == 200 else f"HTTP {status}"
        print(f"[{marca}] {titulo}")
        print(f"         {rota}")
        texto = json.dumps(corpo, ensure_ascii=False, indent=2) if isinstance(corpo, (dict, list)) else str(corpo)
        for linha in texto.splitlines():
            print(f"         {linha}")
        print()
        houve_falha = houve_falha or status >= 400

    if houve_falha:
        print(
            "Alguma consulta não respondeu 200. Se o município aparece sem convênio,\n"
            "ou o serviço não consta na competência, a emissão pela NFS-e Nacional\n"
            "vai continuar sendo recusada com E0312 — nesse caso o caminho é o\n"
            "sistema próprio da prefeitura, ou outro código de serviço."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
