# Roteiro de testes — Emissor de NFS-e

Passo a passo para validar o emissor no Windows, do zero até a primeira nota
real. Feito para ser seguido na ordem, marcando cada caixa.

**Duas coisas para saber antes de começar:**

1. **A simulação também assina a nota.** Não existe modo que dispense
   certificado. Por isso a Etapa A usa um certificado de teste gerado por
   você mesmo.
2. **Certificado de teste não transmite.** A Sefin Nacional exige certificado
   ICP-Brasil de verdade no handshake **inclusive em homologação**. Com o
   certificado de teste você valida tudo, menos transmitir. Não há atalho.

| Etapa | O que faz | Precisa do A1 real? | Tempo |
|---|---|---|---|
| **A** | Instalar e validar o app inteiro, até a simulação | Não | ~40 min |
| **B** | Conseguir o Certificado Digital A1 | — | dias |
| **C** | Emitir de verdade, em homologação e depois em produção | Sim | ~1 h |
| **D** | Gerar o instalador e configurar a segunda máquina | Sim | ~1 h |

> **Sobre os comandos:** foram escritos para o Prompt de Comando do Windows.
> O aplicativo e os testes automatizados foram executados e conferidos em
> Linux; a tradução dos comandos para Windows é revisão de texto, não
> execução. Se algum comando reclamar, o problema mais provável é o PATH do
> Python — veja **Se der errado**, no fim.

---

# Etapa A — validar tudo, hoje, sem o certificado real

## A1. Instalar o Python

- [ ] Baixe o Python 3.11 ou mais novo em <https://www.python.org/downloads/>
- [ ] Na primeira tela do instalador, **marque "Add python.exe to PATH"**

Essa caixa é a causa número um de `python não é reconhecido como um comando`
depois. Se esquecer, reinstale — é mais rápido que consertar o PATH à mão.

Confira:

```bat
python --version
```

Deve responder `Python 3.11.x` ou superior.

## A2. Preparar o projeto

Abra o Prompt de Comando na pasta do projeto e rode:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

O prompt passa a mostrar `(.venv)` no começo da linha. **Toda vez que abrir um
Prompt novo, rode `.venv\Scripts\activate` antes dos outros comandos.**

## A3. Rodar os testes automatizados

```bat
pip install pytest
python -m pytest tests -q
```

- [ ] Termina com `111 passed`

Isso confirma que o ambiente está inteiro antes de você clicar em qualquer
coisa. Se falhar aqui, não adianta seguir — o problema é de instalação.

## A4. Gerar o certificado de teste

```bat
python tools\gerar_certificado_teste.py --cnpj SEU_CNPJ_AQUI
```

- [ ] Aparece `CERTIFICADO DE TESTE GERADO`
- [ ] O arquivo `certificado-teste.pfx` foi criado na pasta
- [ ] Anote a senha mostrada (padrão: `teste123`)

O titular sai como `NAO USAR EM PRODUCAO - CERTIFICADO DE TESTE`. Isso é de
propósito: você vai ver esse texto na tela e não vai confundir com o A1 real.

## A5. Abrir o aplicativo

```bat
python -m app
```

- [ ] A janela do Prompt mostra o endereço `http://127.0.0.1:8765/?t=...`
- [ ] O navegador abre sozinho no **Painel**
- [ ] Há uma **faixa verde** no topo escrito `HOMOLOGAÇÃO`
- [ ] Aparece o aviso amarelo `Configuração incompleta`

**Deixe essa janela do Prompt aberta.** Fechá-la encerra o aplicativo.

## A6. Configurar

Vá em **Configuração** e preencha:

| Campo | O que colocar |
|---|---|
| Ambiente | Homologação |
| Arquivo do certificado | `certificado-teste.pfx` |
| Senha do certificado | `teste123` |
| CNPJ | o CNPJ da sua empresa |
| Inscrição Municipal | o CCM da prefeitura (deixe vazio se não tiver) |
| Série da DPS | `1` |
| Pasta de backup | uma pasta sua, ex.: `C:\Users\voce\OneDrive\NFS-e` |

- [ ] Clicou em **Salvar configuração** e apareceu `Configuração salva`
- [ ] Clicou em **Testar certificado** e apareceu a validade em verde

Se o teste do certificado falhar, a senha está errada ou o arquivo não foi
enviado. Reenvie o arquivo e a senha juntos.

> Se aparecer um aviso amarelo dizendo que a senha **não pôde ser guardada no
> cofre do sistema**, o resto foi salvo normalmente — só a senha vale enquanto
> o app estiver aberto. Em Windows isso é incomum; se acontecer, anote para
> falar com o TI depois. Não impede a Etapa A.

## A7. Cadastrar dois clientes

Vá em **Clientes → Novo cliente** e cadastre **dois**, diferentes de propósito:

**Cliente 1 — recebe por e-mail e tem endereço completo**

| Campo | Valor |
|---|---|
| CNPJ | `11.222.333/0001-81` |
| Razão social | `Cliente Alfa Tecnologia LTDA` |
| E-mail | um e-mail seu, para você receber o teste |
| Logradouro / Número | `Avenida Rio Branco` / `156` |
| Bairro / Município / UF / CEP | `Centro` / `3304557` / `RJ` / `20040901` |
| Cliente ativo | marcado |
| Receber a NFS-e por e-mail | marcado |

**Cliente 2 — ativo, mas NÃO recebe por e-mail**

| Campo | Valor |
|---|---|
| CNPJ | `11444777000161` |
| Razão social | `Beta Serviços Empresariais S/A` |
| E-mail | deixe vazio |
| Município / UF | `3550308` / `SP` |
| Cliente ativo | **marcado** |
| Receber a NFS-e por e-mail | **desmarcado** |

Na lista de clientes, confira o ponto principal do cadastro:

- [ ] O Beta aparece com **Ativo** = `Ativo` e **Recebe por e-mail** = `Não`
- [ ] Clique no botão `Sim` do Alfa: ele vira `Não`, e a coluna **Ativo
      continua `Ativo`**

São duas chaves independentes. Desligar o e-mail não tira o cliente do
faturamento — é para o cliente cujo contador busca o arquivo direto.

- [ ] Volte o Alfa para `Sim` antes de continuar

## A8. Importar a planilha

Gere a planilha de exemplo:

```bat
python tools\gerar_planilha_exemplo.py
```

Vá em **Importar planilha**, escolha o `faturamento_exemplo.xlsx` e clique em
**Conferir planilha**. Na grade de conferência:

- [ ] As duas linhas aparecem em **verde**, marcadas como `válida`
- [ ] A linha do Beta tem a etiqueta **`sem e-mail`**
- [ ] A linha do Beta tem a etiqueta **`completado`** — o endereço que faltava
      na planilha veio do cadastro
- [ ] **Nada foi transmitido** nesta tela

O preenchimento automático importa: endereço incompleto do tomador é causa
comum de rejeição, e planilha de faturamento raramente traz endereço.

Para ver a validação funcionando, abra a planilha no Excel, troque um dígito
do CNPJ de uma linha, salve e importe de novo:

- [ ] A linha aparece em **vermelho**, com o motivo `dígito verificador
      inválido`
- [ ] Desfaça a alteração e importe de novo

## A9. Simular

Vá em **Emitir**. Confira que o botão **Simular (não transmite)** está em azul,
ao lado do de emitir. Clique nele.

- [ ] A barra de progresso anda até o fim
- [ ] O log mostra `DRY-RUN: DPS assinada salva em ...` para as duas linhas
- [ ] O resumo diz `2 pulada(s)` — simulação não consome numeração
- [ ] Os arquivos existem em `notas\dry-run\`

## A10. Validar o XML contra o schema oficial

Este é o passo que mais evita rejeição depois.

1. Baixe o pacote de schemas na documentação técnica de
   <https://www.nfse.gov.br/> e localize o `DPS_v1.00.xsd`
2. Rode:

```bat
python tools\validar_xml.py "notas\dry-run\*.xml" caminho\para\DPS_v1.00.xsd
```

- [ ] Todas as linhas saem como `[OK]`

Se sair `[INVALIDO]`, a ferramenta diz **em que linha e em que campo**. Erro
aqui é problema de layout ou de configuração — e é muito melhor descobrir
agora do que na transmissão.

## A11. Testar a nota avulsa

Vá em **Nota avulsa**, escolha **Beta Serviços** no seletor.

- [ ] Aparece o aviso: *"Este cliente está marcado para NÃO receber por
      e-mail. A nota será emitida e arquivada, sem envio."*

Preencha valor `8.900,00` e uma descrição, e clique em **Simular**.

- [ ] O log mostra a DPS simulada salva

## A12. Testar o envio de e-mail

Ainda em **Configuração**, na seção de e-mail:

| Campo | O que colocar |
|---|---|
| Enviar a nota por e-mail | marcado |
| Servidor SMTP / Porta | `smtp.gmail.com` / `587` |
| Usuário e remetente | seu e-mail |
| Senha SMTP | **Senha de app**, não a senha da conta |
| Permitir envio em homologação | marcado |
| **Redirecionar todos os e-mails para** | **seu próprio e-mail** |

No Gmail e no Google Workspace a senha da conta não funciona. Gere uma Senha
de app em Conta Google → Segurança → Verificação em duas etapas → Senhas de app.

- [ ] Salve e clique em **Enviar e-mail de teste**
- [ ] A mensagem chega na sua caixa

Enquanto **Redirecionar todos os e-mails** estiver preenchido, **nenhum
cliente recebe nada**. Mantenha assim até a Etapa C terminar.

## A13. Testar a trava de duplicidade

Volte em **Emitir** e clique em **Simular** de novo, com as mesmas linhas.

- [ ] Roda normalmente (simulação não registra emissão, então não há o que
      duplicar)

A trava de verdade só age sobre nota transmitida — você vai vê-la funcionando
na Etapa C.

---

**Fim da Etapa A.** O aplicativo está validado em tudo que não depende da
Receita. Agora falta o certificado.

---

# Etapa B — conseguir o Certificado Digital A1

## O que comprar

Três coisas precisam estar certas, e a segunda é irreversível:

| Item | O certo | Por quê |
|---|---|---|
| Tipo de titular | **e-CNPJ** | Quem emite a nota é a empresa, não a pessoa |
| Mídia | **A1** (arquivo) | **A3 é token/cartão e NÃO funciona com este sistema** |
| Validade | 1 ano | A1 é anual; renovar é comprar de novo |

> **Não compre A3.** O A3 vive num token USB ou cartão e exige um protocolo
> diferente (PKCS#11) que este código não implementa. Se comprar A3 por engano,
> não há conserto no software — é comprar de novo.

## Como comprar

1. Escolha uma **Autoridade Certificadora credenciada pela ICP-Brasil**.
   Existem várias no mercado (Certisign, Serasa Experian, Serpro, Valid,
   Soluti, Safeweb, entre outras). Compare preço e prazo de atendimento.
2. Compre o **e-CNPJ A1** no site da AC escolhida.
3. Agende a validação presencial ou por videoconferência.
4. Apresente os documentos.
5. Baixe o certificado.

**Faixa de preço observada no mercado:** algo entre R$ 200 e R$ 450 por ano.
Trate como ordem de grandeza, não como cotação — varia bastante entre AC e
período.

**Documentos normalmente exigidos da pessoa jurídica:** cartão CNPJ, contrato
social ou requerimento de empresário, documento de identidade e CPF do
representante legal, e comprovante de endereço. Se quem faz a validação não for
o representante legal, é preciso procuração. **Confirme a lista com a AC
escolhida** — cada uma tem a sua.

## A pegadinha do Windows: exigir chave exportável

Várias ACs entregam o A1 **instalando direto no repositório de certificados do
Windows**, e não como arquivo. O aplicativo precisa do arquivo `.pfx`.

- [ ] Na compra, peça o A1 **com chave privada exportável**, ou o download
      direto em `.pfx`

Se já veio instalado no Windows, exporte assim:

1. Tecle `Win + R`, digite `certmgr.msc` e Enter
2. Vá em **Pessoal → Certificados**
3. Botão direito no seu certificado → **Todas as tarefas → Exportar**
4. Escolha **"Sim, exportar a chave privada"**
5. Formato **`.PFX`**, defina uma senha forte e salve

> Se a opção **"Sim, exportar a chave privada"** estiver acinzentada, a chave
> foi marcada como não exportável na emissão. **Não há como recuperar** — é
> caso de falar com a AC. Por isso o pedido na compra importa.

## Em paralelo, o que não é certificado

Enquanto espera, resolva o que o aplicativo não resolve por você:

- [ ] Confirmar com o contador ou com a Prefeitura do Rio se há credenciamento
      a fazer no sistema nacional, e qual é a Inscrição Municipal da empresa
- [ ] Fechar com o contador: **cTribNac** (o padrão é `170101`, consultoria),
      **regime tributário** e **alíquota do ISS**

O app não valida escolha fiscal. Código de tributação errado numa tela bonita
continua sendo código errado.

---

# Etapa C — emitir de verdade

## C1. Trocar o certificado

Em **Configuração**:

- [ ] Envie o `.pfx` real e a senha dele
- [ ] Clique em **Testar certificado** — o titular agora deve mostrar o nome da
      **sua empresa**, não `NAO USAR EM PRODUCAO`
- [ ] Confirme que o **Ambiente continua Homologação** (faixa verde)
- [ ] Apague o `certificado-teste.pfx` da pasta

## C2. Emitir UMA nota em homologação

Não comece pelo lote. Importe a planilha, e em **Emitir**:

- [ ] Desmarque todas as linhas, deixando **uma só** marcada
- [ ] Clique em **Emitir em homologação**

**Se autorizar**, confira tudo o que deveria ter acontecido:

- [ ] O log mostra `AUTORIZADA` e a chave de acesso
- [ ] Em `notas\ANO\MES\` existem: `_nfse.xml`, `_dps-assinada.xml`,
      `_danfse.pdf` e `_retorno.json`
- [ ] A nota aparece na tela **Histórico**
- [ ] O e-mail chegou (no endereço de redirecionamento, não no do cliente)
- [ ] A pasta de backup recebeu os arquivos **e** o `controle_..._serie1.json`
- [ ] O Painel mostra a data do **último backup**

**Se rejeitar**, a mensagem traz o código e a descrição do governo. Os mais
comuns:

| O que a mensagem cita | Onde resolver |
|---|---|
| `cTribNac` / código de serviço | Configuração → Serviço prestado (confirme com o contador) |
| Endereço ou município do tomador | Cadastro do cliente, ou colunas da planilha |
| Inscrição municipal | Configuração → Prestador |
| Certificado / assinatura | Certificado errado, vencido, ou ainda o de teste |
| Regime tributário | Configuração → Prestador (confirme com o contador) |

Rejeição **não consome numeração** — corrija e emita de novo sem se preocupar
com o número.

## C3. Testar a trava de duplicidade

Com a nota já autorizada, tente emitir a **mesma linha** de novo:

- [ ] O resultado sai como `PULADA`, mostrando a chave da emissão original
- [ ] **Nada foi transmitido**

É essa trava que impede emitir o mesmo lote duas vezes por engano.

## C4. Lote completo em homologação

- [ ] Emita o restante das linhas
- [ ] Confira o relatório CSV em `logs\relatorio_*.csv`

## C5. Passar para produção

Só depois de tudo acima:

- [ ] XML validado contra o XSD oficial (passo A10)
- [ ] `cTribNac`, regime e alíquota confirmados **com o contador**
- [ ] Situação do município confirmada
- [ ] Lote em homologação emitido e conferido
- [ ] **Ajuste `Próximo número livre`** para o próximo número da sua série, se
      a empresa já emitiu notas por outro meio
- [ ] **Apague o "Redirecionar todos os e-mails"** — agora os clientes recebem
- [ ] Em Configuração, mude o Ambiente para **Produção**

Confira que a faixa do topo ficou **vermelha**. Em Emitir, o botão pede que
você digite `EMITIR EM PRODUCAO` por extenso, mostrando antes a quantidade de
notas e o valor total.

- [ ] Emita **uma nota só** na primeira vez, e confira tudo de novo

> Nota emitida em produção só se desfaz por cancelamento formal, com prazo e
> regra do município.

---

# Etapa D — instalador e segunda máquina

Gerar o `.exe` e o instalador: siga
[`empacotamento/LEIA-ME.md`](empacotamento/LEIA-ME.md).

**Ao instalar no segundo laptop, a única coisa que não pode errar:**

| Computador | Série da DPS |
|---|---|
| Laptop 1 | `1` |
| Laptop 2 | `2` |
| Laptop 3 | `3` |

`nDPS` é único **por série**. Duas máquinas na mesma série produzem numeração
repetida. O app grava o nome da máquina no arquivo de controle e **bloqueia a
emissão** se perceber que o controle veio de outro computador.

- [ ] Defina a série logo depois de instalar, antes de emitir qualquer coisa
- [ ] Leve o cadastro de clientes com **Clientes → Exportar** e **Importar**

Não coloque a pasta de dados do app no OneDrive: o banco do cadastro é SQLite,
e sincronização de arquivo aberto corrompe. A pasta de **backup** no OneDrive
é outra coisa — ali é cópia, e pode.

---

# Se der errado

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `python não é reconhecido` | Instalou sem marcar "Add to PATH" | Reinstale o Python marcando a caixa |
| `pip não é reconhecido` | Esqueceu de ativar o ambiente | Rode `.venv\Scripts\activate` |
| Falha no handshake mTLS | Certificado de teste, vencido, revogado, ou sem cadeia ICP-Brasil | Confira em **Testar certificado**. Nunca desative a verificação TLS |
| Aviso de cofre de senhas indisponível | Máquina sem Gerenciador de Credenciais acessível | O app funciona; a senha vale só na sessão. Fale com o TI |
| Página com `403` no navegador | Aba antiga, com token de uma sessão anterior | Feche a aba e reabra pelo atalho |
| A porta 8765 já está em uso | Outra cópia do app aberta | Feche a outra janela, ou rode `python -m app --porta 8790` |
| `Autenticação SMTP recusada` | Usou a senha da conta no Gmail | Gere uma **Senha de app** |
| Antivírus barra o `.exe` | Falso-positivo comum com PyInstaller | Veja `empacotamento/LEIA-ME.md`; peça exceção ao TI |
| Emissão bloqueada por conflito de máquina | Controle de numeração veio de outro computador | Troque a série na Configuração. Só assuma o controle se a outra máquina não emitir mais nessa série |
| `Configuração incompleta` não sai | Falta CNPJ, certificado ou município | A mensagem lista exatamente o que falta |

---

# O que este roteiro foi conferido

A Etapa A inteira foi executada no navegador durante o desenvolvimento:
configuração, teste do certificado, cadastro com as duas chaves, importação com
preenchimento pelo cadastro, simulação e validação do XML. Os 111 testes
automatizados cobrem as travas de segurança, a numeração e o fluxo de emissão.

**O que não foi possível conferir aqui, e você vai ser o primeiro a ver:**

- Os comandos rodando no Windows de verdade (o desenvolvimento foi em Linux)
- O Gerenciador de Credenciais do Windows guardando as senhas
- A geração do `.exe` (PyInstaller não faz compilação cruzada)
- **Qualquer transmissão real para a Sefin Nacional**, em qualquer ambiente

Se algum passo não bater com o que está escrito aqui, anote o que apareceu na
tela — a diferença é informação útil, não erro seu.
