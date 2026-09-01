# Emissor automatizado de NFS-e Nacional

Automatiza a emissão de Notas Fiscais de Serviço Eletrônicas (NFS-e) a partir de
uma planilha `faturamento.xlsx`, usando a API da **NFS-e Nacional (padrão
federal / Sefin Nacional)** e Certificado Digital **A1** via mTLS. Ao final,
cada nota autorizada é **enviada por e-mail ao cliente** com o DANFSe (PDF) e o
XML em anexo.

---

## ⚠️ Leia isto antes de tudo

**1. A API não recebe a DPS como JSON.**
O pedido original falava em "montar o JSON da DPS". O contrato real da Sefin
Nacional é outro:

```
POST https://sefin.nfse.gov.br/sefinnacional/nfse
Content-Type: application/json

{"dpsXmlGZipB64": "<base64( gzip( XML da DPS assinado em XMLDSig ) )>"}
```

O JSON é só o **envelope**. O conteúdo é o XML da DPS, assinado digitalmente,
comprimido em GZip e codificado em Base64. Este projeto monta a DPS como um
dicionário Python (`nfse/dps.py` → sua "visão JSON", fácil de inspecionar e
testar) e o serializa no XML exigido, na ordem do schema.

**2. Valide o layout contra o XSD oficial.**
A ordem e a obrigatoriedade dos campos vêm do `DPS_v1.00.xsd`. Baixe o pacote de
schemas na área técnica de <https://www.nfse.gov.br/> e valide o XML gerado pelo
`--dry-run` antes de emitir em produção. Layouts são revisados; se o seu vier em
versão diferente, ajuste `VERSAO_LAYOUT` em `nfse/config.py`.

**3. Confirme os parâmetros fiscais com o seu contador.**
`cTribNac`, `opSimpNac`, `regEspTrib`, `tribISSQN`, `tpRetISSQN` e a alíquota do
ISS determinam quanto imposto você paga. O padrão aqui é `170101` (LC 116,
subitem 17.01 — assessoria/consultoria), Simples Nacional, ISS não retido. Não
assuma que serve para o seu caso.

**4. Confirme a adesão do seu município.**
O Rio de Janeiro (IBGE `3304557`) opera no padrão nacional, mas municípios
conveniados têm regras próprias de emissor e de convênio. Consulte a situação
atual na Consulta Pública do portal nacional antes de ir para produção.

---

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.exemplo .env               # e preencha o .env
```

## Convertendo o Certificado A1

Você recebe da Autoridade Certificadora um arquivo **`.pfx`** (ou `.p12`) —
formato PKCS#12, que embala certificado + chave privada protegidos por senha.

### Opção A — usar o `.pfx` direto (recomendada)

Nada a converter. No `.env`:

```ini
CERT_PFX=certificado.pfx
CERT_PFX_SENHA=sua-senha
```

O módulo `nfse/certificado.py` abre o PKCS#12 em memória com a biblioteca
`cryptography` e injeta o par no `SSLContext`. **A chave decifrada nunca fica em
disco de forma persistente.** É o caminho mais seguro.

### Opção B — converter para PEM (`certificado.crt` + `chave.key`)

Se você preferir os arquivos separados, use o OpenSSL:

```bash
# 1) Certificado público (sem a chave)
openssl pkcs12 -in certificado.pfx -clcerts -nokeys -out certificado.crt

# 2) Chave privada — MANTENDO a senha (recomendado)
openssl pkcs12 -in certificado.pfx -nocerts -out chave.key

# 3) Chave privada SEM senha (só se realmente precisar; arquivo mais exposto)
openssl pkcs12 -in certificado.pfx -nocerts -nodes -out chave.key

# 4) Restrinja a permissão do arquivo (Linux/macOS)
chmod 600 chave.key
```

No `.env`, deixe `CERT_PFX` vazio e preencha:

```ini
CERT_CRT=certificado.crt
CERT_KEY=chave.key
CERT_KEY_SENHA=senha-da-chave     # deixe vazio se usou a etapa 3
```

> **Por que não `requests.post(..., cert=("certificado.crt", "chave.key"))`?**
> Porque essa API do `requests` **não aceita chave com senha**. Por isso o
> projeto monta o `SSLContext` manualmente — assim funciona nos dois formatos,
> com ou sem senha, sem obrigar você a deixar a chave decifrada no disco.

### Conferindo o certificado

```bash
openssl pkcs12 -in certificado.pfx -nokeys -info | openssl x509 -noout -subject -dates
```

O script já valida vigência na largada e avisa quando faltam menos de 30 dias
para o vencimento.

---

## Endpoints: homologação × produção

Trocados pela variável `AMBIENTE` no `.env` (ou `--ambiente` na linha de comando):

| `AMBIENTE`     | URL base                                                | `tpAmb` | Efeito |
|----------------|---------------------------------------------------------|---------|--------|
| `homologacao`  | `https://sefin.producaorestrita.nfse.gov.br/SefinNacional` | 2 | Produção restrita: notas **sem valor fiscal** |
| `producao`     | `https://sefin.nfse.gov.br/sefinnacional`                  | 1 | Notas **com valor fiscal real** |

Rotas usadas: `POST /nfse` (emissão), `GET /nfse/{chave}` (consulta),
`GET /danfse/{chave}` (PDF). Estão em `nfse/config.py` — se o layout mudar,
altere lá, num lugar só.

**Sempre valide em homologação antes de virar a chave.** NFS-e emitida em
produção só se desfaz por cancelamento formal, com prazo e regra municipal.

---

## A planilha

`faturamento.xlsx`, primeira aba, com o cabeçalho na linha 1.

**Colunas obrigatórias:** `CNPJ_Cliente`, `Razao_Social`, `Email_Cliente`,
`Valor_Servico`, `Descricao_Servico`.

**Colunas opcionais** (qualificam melhor o tomador e reduzem rejeições):
`Logradouro`, `Numero`, `Complemento`, `Bairro`, `Cod_Municipio` (IBGE, 7
dígitos), `UF`, `CEP`, `Telefone`.

Detalhes do parser:

- `CNPJ_Cliente` aceita máscara (`11.222.333/0001-81`) e valida o dígito
  verificador. Um CPF de 11 dígitos também é aceito, e a DPS sai com `<CPF>`.
- `Valor_Servico` aceita `4500.00`, `"4.500,00"` e `"R$ 4.500,00"`.
- `Descricao_Servico` é limitada a 2000 caracteres (limite de `xDescServ`).

Gere um modelo com:

```bash
python tools/gerar_planilha_exemplo.py
```

---

## Uso

```bash
# 1) Simule: monta e assina a DPS, salva o XML em notas/dry-run/, NÃO transmite
python -m nfse.main --dry-run

# 2) Emita em homologação
python -m nfse.main

# 3) Emita em produção (notas reais)
python -m nfse.main --ambiente producao

# Outras opções
python -m nfse.main --competencia 2026-08   # mês de referência do serviço
python -m nfse.main --linhas 2,5,7          # só estas linhas do Excel
python -m nfse.main --sem-pdf               # não baixa a DANFSe
python -m nfse.main --sem-email             # não envia ao cliente nesta execução
python -m nfse.main --reemitir              # ignora o controle de duplicidade
```

O código de saída é `0` quando tudo foi autorizado e `1` se houve qualquer
falha — útil para agendar no cron ou no Agendador de Tarefas.

---

## Envio automático ao cliente

Toda nota autorizada é enviada ao endereço da coluna `Email_Cliente`, com o
DANFSe (PDF) e o XML da NFS-e em anexo. Configure na seção "E-mail" do `.env`:

```ini
EMAIL_ENVIAR=true
EMAIL_SMTP_SERVIDOR=smtp.gmail.com
EMAIL_SMTP_PORTA=587
EMAIL_SMTP_USUARIO=voce@suaempresa.com.br
EMAIL_SMTP_SENHA=sua-senha-de-app
EMAIL_REMETENTE=voce@suaempresa.com.br
EMAIL_REMETENTE_NOME=Consultoria Mari Torres
EMAIL_BCC=contador@escritorio.com.br      # opcional
```

**Gmail / Google Workspace:** a senha da conta não funciona. Gere uma
**Senha de app** em Conta Google → Segurança → Verificação em duas etapas →
Senhas de app.

### Duas travas de segurança

1. **Em homologação o envio é bloqueado por padrão.** Notas de teste não têm
   valor fiscal; mandá-las ao cliente real só gera confusão. Para testar o
   fluxo de e-mail, use `EMAIL_PERMITIR_HOMOLOGACAO=true`.
2. **`EMAIL_TESTE_DESTINO`** redireciona *todos* os e-mails para o endereço
   informado. Aponte para a sua própria caixa enquanto valida o modelo da
   mensagem — nenhum cliente recebe nada.

Falha no envio **não invalida a nota**: ela já está autorizada na Sefin. O erro
aparece na coluna `email` do relatório CSV para você reenviar depois.

Personalize a mensagem com `EMAIL_ASSUNTO` e `EMAIL_CORPO`. Variáveis
disponíveis: `{tomador}`, `{prestador}`, `{competencia}`, `{descricao}`,
`{valor}`, `{chave}`, `{chave_curta}`.

---

## Interface gráfica (aplicativo no laptop)

Além da linha de comando, o emissor tem uma interface para a pessoa responsável
operar no dia a dia. Ela roda **no próprio laptop**: sobe um servidor em
`127.0.0.1` e abre no navegador. O certificado A1 nunca sai da máquina.

```bash
python -m app                    # abre o navegador automaticamente
python -m app --sem-navegador    # só sobe o servidor
python -m app --porta 9000       # porta fixa
```

A configuração vai para a pasta de dados do usuário — `%APPDATA%\EmissorNFSe`
no Windows — e **não usa mais o `.env`**. As senhas do certificado e do SMTP
ficam no cofre de credenciais do sistema (Gerenciador de Credenciais no Windows),
não em arquivo de texto.

### As quatro telas

| Tela | Para quê |
|---|---|
| **Painel** | Ambiente, série da DPS, validade do certificado e situação do e-mail, tudo à vista antes de faturar. |
| **Importar planilha** | Envia o `.xlsx` e mostra a grade de conferência: linhas válidas em verde, inválidas em vermelho com o motivo exato. **Nada é transmitido nesta tela.** |
| **Emitir** | Competência, seleção de linhas, botão de simular ao lado do de emitir, barra de progresso e log ao vivo. |
| **Nota avulsa** | Uma nota só, digitada na hora, escolhendo o cliente do cadastro. |
| **Clientes** | Cadastro com endereço, e as duas chaves explicadas abaixo. |
| **Histórico** | Notas já emitidas, com busca por cliente, CNPJ ou chave de acesso. |

### Travas de segurança

**Contra emitir em produção por engano:**
- Faixa de cor permanente no topo — verde em homologação, vermelha em produção.
- Em produção, um modal exige digitar `EMITIR EM PRODUCAO` por extenso, mostrando
  antes a quantidade de notas e o valor total.
- O botão **Simular** tem o mesmo destaque do de emitir, e é o primeiro da linha.
- Dois lotes simultâneos são recusados: numeração de DPS não suporta concorrência.

**Contra abuso do servidor local:**
- Escuta apenas em `127.0.0.1`. Nunca em `0.0.0.0` — isso exporia o aplicativo à
  rede local.
- Toda rota que altera estado exige um token aleatório, gerado a cada
  inicialização. Sem isso, **qualquer página aberta no navegador conseguiria
  disparar uma emissão** por CSRF contra o `localhost`.
- O header `Host` é validado, o que barra ataque de DNS rebinding.

### Uma série de DPS por computador

`nDPS` é único **por série**. Com 2 a 4 laptops emitindo, dê a cada máquina uma
série diferente (1, 2, 3...) na tela de Configuração. Duas máquinas na mesma
série geram numeração repetida — e número repetido é rejeição na melhor das
hipóteses.

O aplicativo grava o nome da máquina no arquivo de controle e **bloqueia a
emissão** se detectar que o controle veio de outro computador (acontece ao
restaurar backup na máquina errada, por exemplo). A saída correta é trocar a
série; assumir o controle é uma ação explícita, para o caso de a outra máquina
realmente não emitir mais naquela série.

Na linha de comando o mesmo bloqueio vale, e `--assumir-maquina` é o
equivalente do botão.

### Cadastro de clientes: duas chaves, não uma

Cada cliente tem **dois interruptores independentes**:

| Chave | O que controla |
|---|---|
| **Ativo** | Se o cliente entra no faturamento e aparece na nota avulsa |
| **Recebe por e-mail** | Se a NFS-e é enviada automaticamente para ele |

São separadas de propósito. Um cliente pode pedir para **não** receber por
e-mail — o contador dele busca o arquivo direto — e continuar sendo faturado
normalmente. Com uma chave só, desligar o e-mail o tiraria do faturamento.
E um cliente que encerrou contrato sai da lista sem ninguém precisar lembrar
de mexer no e-mail dele.

Quando o cliente está marcado para não receber, a nota é **emitida e arquivada
normalmente** — só não sai e-mail, e o relatório registra isso.

O cadastro também **completa o que falta na planilha**: se a linha não trouxer
endereço, ele vem do cliente cadastrado (endereço incompleto do tomador é causa
comum de rejeição). O que veio digitado na planilha tem precedência.

Para levar o cadastro a outro laptop, use **Exportar** e **Importar** (JSON).
É manual de propósito: colocar o banco SQLite numa pasta do OneDrive ou do
Drive corrompe o arquivo, porque a sincronização copia banco aberto.

### Backup automático

Instalar num laptop cria um ponto único de falha: se a máquina morrer, vão
junto as notas **e** o `controle_*.json` — e perder o controle é perder a
sequência da numeração.

Aponte uma pasta na Configuração (OneDrive, Drive ou rede servem — aqui é cópia
de arquivo, não banco aberto). A cada nota autorizada, os arquivos dela e o
controle de numeração são copiados para lá. O painel mostra a data do último
backup, e o aplicativo avisa em vermelho enquanto isso estiver desligado.

Falha no backup **não invalida a nota**: ela já está autorizada e gravada
localmente. Vira aviso no log.

### Empacotar como instalador Windows

Veja [`empacotamento/LEIA-ME.md`](empacotamento/LEIA-ME.md). Resumo:

```bat
pip install pyinstaller
pyinstaller empacotamento\emissor.spec --noconfirm
```

e depois compile `empacotamento\instalador.iss` no Inno Setup.

> **Precisa de uma máquina Windows.** O PyInstaller não faz compilação cruzada.
> O `LEIA-ME` também cobre o falso-positivo de antivírus, que é comum com
> binário empacotado.

---

## O que o script produz

```
notas/
└── 2026/
    └── 09-setembro/
        ├── <chave>_<cnpj>_nfse.xml           # XML da NFS-e devolvido pela Sefin
        ├── <chave>_<cnpj>_dps-assinada.xml   # a DPS transmitida (prova de auditoria)
        ├── <chave>_<cnpj>_danfse.pdf         # DANFSe
        ├── <chave>_<cnpj>_retorno.json       # metadados da emissão
        └── rejeitadas/
            └── linha003_<cnpj>_<hora>_erro.json

logs/
├── emissao_20260901-161317.log               # log detalhado (DEBUG)
├── relatorio_20260901-161317.csv             # uma linha por linha da planilha
└── controle_homologacao_serie1.json          # numeração + notas já emitidas
```

O CSV (separador `;`, abre direto no Excel) traz por linha: `linha_planilha`,
`situacao`, `documento_tomador`, `razao_social`, `valor_servico`, `numero_dps`,
`chave_acesso`, `detalhe`, `email`, `arquivo_xml`.

Situações possíveis:

| Situação     | Significado |
|--------------|-------------|
| `AUTORIZADA` | NFS-e emitida; arquivos salvos e e-mail enviado |
| `REJEITADA`  | A Sefin recusou; `detalhe` traz código e descrição do governo |
| `INVALIDA`   | A linha não passou na validação local (CNPJ, valor, descrição) |
| `ERRO_LOCAL` | Falha do script/rede antes ou durante o envio |
| `PULADA`     | Já emitida antes, filtrada por `--linhas`, ou dry-run |

---

## Controle de duplicidade e numeração

`nDPS` precisa ser **único e sequencial** por série. Rodar o script duas vezes
sobre a mesma planilha, sem controle, emitiria a mesma nota de novo — e cancelar
NFS-e dá trabalho.

`logs/controle_<ambiente>_serie<N>.json` guarda o último número usado e a
impressão digital (`CNPJ + valor + descrição + competência`) de cada linha já
emitida. Linhas repetidas são puladas com a chave da emissão original. O arquivo
é gravado **a cada nota**, então uma queda no meio do lote não perde a numeração.

- Se você já emite notas por outro meio, ajuste `DPS_NUMERO_INICIAL` para o
  próximo número livre da sua série antes da primeira execução.
- **Faça backup desse arquivo junto com as notas.** Perdê-lo significa perder o
  controle da sequência.
- `--reemitir` ignora a trava. Use conscientemente.

---

## Estrutura do projeto

| Arquivo | Responsabilidade |
|---|---|
| `nfse/config.py` | Endpoints, credenciais (via `.env`), parâmetros fiscais |
| `nfse/planilha.py` | Leitura e validação do `.xlsx` (CNPJ/CPF, valores, descrição) |
| `nfse/dps.py` | Monta a DPS como dicionário e serializa no XML do layout |
| `nfse/assinatura.py` | XMLDSig enveloped + GZip + Base64 |
| `nfse/certificado.py` | Carrega o A1 (`.pfx` ou PEM) e monta a sessão mTLS |
| `nfse/cliente.py` | Chamadas REST, retentativa e normalização do retorno |
| `nfse/armazenamento.py` | Grava XML/PDF/metadados em pastas por mês |
| `nfse/email_envio.py` | Envio SMTP da nota ao cliente |
| `nfse/estado.py` | Numeração da DPS e proteção contra duplicidade |
| `nfse/logs.py` | Logger e relatório CSV |
| `nfse/main.py` | Orquestração e CLI |

---

## Testes

```bash
pip install pytest
python -m pytest tests -q
```

Cobrem: montagem da DPS, ordem dos elementos no XML, `Id` de 45 posições,
assinatura XMLDSig (com certificado autoassinado gerado na hora), empacotamento
GZip+Base64, parsing da planilha, normalização do retorno da API e as travas do
envio por e-mail.

---

## Segurança

- `.pfx`, `.key`, `.crt`, `.pem`, `.env`, `notas/` e `logs/` estão no
  `.gitignore`. **Nunca versione certificado, senha ou dado fiscal de cliente.**
- Nenhuma credencial está hardcoded: tudo vem do `.env`.
- A verificação TLS do servidor permanece ativa. Se der erro de handshake, o
  problema é o seu certificado (vencido, revogado, cadeia ICP-Brasil ausente) —
  **não desative a verificação**.
- Rejeições `4xx` nunca são reenviadas automaticamente: repetir uma emissão
  recusada só criaria risco de duplicidade. Retentativa existe apenas para falha
  de rede e erro `5xx`.

---

## Antes de emitir em produção — checklist

- [ ] XML do `--dry-run` validado contra o `DPS_v1.00.xsd` oficial
- [ ] `cTribNac` conferido na Consulta de Serviços do portal nacional
- [ ] Regime tributário (`opSimpNac`, `regEspTrib`) e ISS validados com o contador
- [ ] Situação do município (Rio de Janeiro) confirmada no portal nacional
- [ ] Lote emitido com sucesso em homologação, XML de retorno conferido
- [ ] E-mail testado com `EMAIL_TESTE_DESTINO` apontando para a sua caixa
- [ ] `DPS_NUMERO_INICIAL` alinhado com a numeração já usada na sua série
- [ ] Rotina de backup de `notas/` e `logs/` definida
