"""
Textos de ajuda do botão "?" ao lado de cada campo.

A chave é o `id` do campo. Quando o mesmo id existe em telas diferentes com
sentidos diferentes, use "tela:id" (tela = `pagina_atual` do template); essa
forma tem prioridade sobre a chave simples. O app.js põe o "?" sozinho em todo
<label for="..."> que tiver texto aqui — campo novo só precisa de uma entrada.

Escreva para quem não é contador nem programador: o que é, de onde tirar,
e um exemplo.
"""

from __future__ import annotations

AJUDA: dict[str, str] = {
    # ---------------- Configuração: ambiente e certificado ----------------
    "ambiente": (
        "Homologação é o modo de teste: as notas não valem nada e servem para treinar. "
        "Produção emite notas de verdade, com valor fiscal. Teste primeiro em homologação."
    ),
    "certificado_arquivo": (
        "É o arquivo do certificado digital A1 da empresa (termina em .pfx ou .p12). "
        "Quem comprou o certificado recebeu esse arquivo por e-mail ou download. "
        "Só precisa enviar de novo quando renovar o certificado."
    ),
    "senha_certificado": (
        "A senha que foi criada junto com o certificado digital. Fica guardada no "
        "cofre do Windows, não em arquivo. Deixe em branco para manter a que já está salva."
    ),
    # ---------------- Configuração: prestador ----------------
    "prestador_cnpj": "O CNPJ da sua empresa, a que emite a nota. Pode digitar com ou sem pontos.",
    "prestador_im": (
        "Inscrição Municipal da sua empresa na prefeitura. Aparece no cartão de inscrição "
        "ou em notas antigas. Se a prefeitura não pedir, deixe em branco."
    ),
    "prestador_cod_municipio": (
        "Código IBGE (7 dígitos) da cidade onde a empresa está registrada. "
        "Rio de Janeiro = 3304557. Outras cidades: pesquise \"código IBGE\" + nome da cidade."
    ),
    "prestador_simples_nacional": (
        "Diz se a empresa é do Simples Nacional. Microempresa ou empresa de pequeno porte "
        "no Simples = \"ME/EPP\". Na dúvida, pergunte ao contador."
    ),
    "prestador_regime_apuracao_sn": (
        "Como os impostos são pagos dentro do Simples. Quase sempre é a opção 1 "
        "(tudo pelo Simples). Só muda se o contador disser."
    ),
    "prestador_regime_especial": (
        "Regimes especiais de algumas profissões (cooperativas, cartórios, autônomos...). "
        "A maioria das empresas usa \"0 — Nenhum\"."
    ),
    # ---------------- Configuração: serviço ----------------
    "servico_ctribnac": (
        "Código do tipo de serviço, com 6 dígitos. O jeito mais fácil: abra uma nota que "
        "você já emitiu no portal gov.br e copie o \"Código de Tributação Nacional\" "
        "(ex.: 17.03.03 → 170303). Pode digitar com pontos."
    ),
    "servico_ctribmun": (
        "Código complementar da prefeitura. No portal gov.br aparece com 9 dígitos "
        "(ex.: 17.03.03.001) — pode colar inteiro, o app guarda só os 3 últimos (001). "
        "O Rio de Janeiro exige este campo."
    ),
    "servico_cod_municipio": (
        "Código IBGE da cidade onde o serviço é prestado. Normalmente é a mesma cidade da "
        "empresa (Rio de Janeiro = 3304557)."
    ),
    "servico_trib_issqn": (
        "Se o serviço paga ISS. Quase sempre é \"1 — Operação tributável\". "
        "As outras opções são casos especiais (exportação, imunidade)."
    ),
    "servico_ret_issqn": (
        "Quem recolhe o ISS. Normalmente é a própria empresa (\"Não retido\"). "
        "Só mude se o cliente desconta o ISS do pagamento."
    ),
    "iss_aliquota": (
        "Percentual do ISS, ex.: 2,00. Empresas do Simples Nacional deixam em branco — "
        "o imposto já vai no percentual do Simples."
    ),
    "servico_ptottribsn": (
        "Percentual total de impostos do Simples Nacional no mês (ex.: 8,63). "
        "O contador informa esse número; ele muda com o faturamento, então atualize todo mês. "
        "Sem ele a nota é recusada."
    ),
    "servico_ind_tot_trib": (
        "Se a nota mostra o valor aproximado de impostos. Para Simples Nacional o app "
        "usa o percentual acima automaticamente; deixe como está."
    ),
    # ---------------- Configuração: numeração e pastas ----------------
    "serie_dps": (
        "Um número que identifica este computador na numeração das notas. Use de 1 a 49999 "
        "(ex.: 1). Se dois computadores emitem, cada um precisa de uma série diferente."
    ),
    "numero_dps_inicial": (
        "Número de controle da próxima nota DESTE app (não é o número da NFS-e, que a "
        "Receita dá). Numa instalação nova, deixe 1."
    ),
    "diretorio_notas": (
        "Onde ficam os arquivos de cada nota (XML e PDF). Em branco, o app usa a própria pasta."
    ),
    "diretorio_logs": (
        "Onde fica o registro do que foi emitido e o controle de numeração. "
        "Em branco, o app usa a própria pasta."
    ),
    "pasta_backup": (
        "Uma pasta do OneDrive, Google Drive ou rede. A cada nota emitida, uma cópia vai "
        "para lá — se o computador quebrar, nada se perde. Ex.: C:\\Users\\voce\\OneDrive\\NFS-e"
    ),
    "recorrencia_modo": (
        "O que fazer quando chega o dia de um cliente com nota mensal. Manual: o app avisa e "
        "espera você clicar. Automático: emite sozinho se o valor for fixo."
    ),
    # ---------------- Configuração: e-mail ----------------
    "email_enviar": "Liga o envio automático da nota por e-mail para o cliente depois de emitida.",
    "email_smtp_servidor": (
        "Endereço do servidor de envio do seu e-mail. Gmail: smtp.gmail.com. "
        "Outlook/Hotmail: smtp-mail.outlook.com. Outros: peça ao provedor."
    ),
    "email_smtp_porta": "Normalmente 587. Alguns provedores usam 465.",
    "email_smtp_usuario": "O seu endereço de e-mail completo, o mesmo do login.",
    "senha_smtp": (
        "A senha do e-mail. No Gmail é uma \"senha de app\" (16 letras), criada em "
        "Conta Google → Segurança → Senhas de app. A senha normal não funciona lá."
    ),
    "email_remetente": "O e-mail que aparece como remetente para o cliente. Normalmente o mesmo do usuário.",
    "email_remetente_nome": "O nome que o cliente vê no e-mail, ex.: o nome da sua empresa.",
    "email_bcc": "Endereços que recebem uma cópia escondida de toda nota enviada, ex.: o do contador.",
    "email_teste_destino": (
        "Para testes: todos os e-mails vão para este endereço em vez do cliente. "
        "Deixe em branco no uso normal."
    ),
    "email_smtp_starttls": "Deixe marcado com a porta 587. Desmarque só se usar a porta 465.",
    "email_permitir_homologacao": (
        "Permite mandar e-mail também das notas de teste, para ver como chega. Use junto com "
        "\"Redirecionar todos os e-mails\", senão a nota de teste chega ao cliente."
    ),
    "email_assunto": "O assunto do e-mail. Os textos entre chaves, como {tomador} ou {competencia}, são trocados pelos dados da nota.",
    "email_corpo": "O texto do e-mail. Os textos entre chaves são trocados pelos dados da nota.",
    # ---------------- Cadastro de cliente ----------------
    "documento": "CNPJ (empresa) ou CPF (pessoa) de quem recebe a nota. Pode digitar com ou sem pontos.",
    "razao_social": "Nome da empresa como está no CNPJ, ou o nome completo da pessoa.",
    "email": "Para onde a nota é enviada automaticamente, se o envio por e-mail estiver ligado.",
    "telefone": "Opcional. Vai para a nota apenas como contato.",
    "ativo": "Cliente inativo não entra no faturamento nem aparece na Nota avulsa. Útil para quem parou de contratar.",
    "receber_por_email": "Marcado: o cliente recebe a nota por e-mail sozinho. Desmarcado: a nota é emitida e só fica arquivada.",
    "logradouro": "Rua, avenida etc. O endereço é opcional; se preencher, informe também número, bairro, cidade e CEP.",
    "numero": "Número do endereço. Sem número, use S/N.",
    "complemento": "Sala, andar, bloco... Opcional.",
    "bairro": "Bairro do endereço.",
    "cod_municipio": "Código IBGE da cidade, 7 dígitos. Rio de Janeiro = 3304557.",
    "uf": "Sigla do estado, ex.: RJ.",
    "cep": "CEP do endereço, só números ou com traço.",
    "dia_emissao_recorrente": (
        "Para clientes que pagam todo mês: o dia em que a nota deve sair (1 a 28). "
        "Deixe em branco se não for mensal."
    ),
    "valor_recorrente": "Valor fixo da nota mensal. Se o valor muda todo mês, deixe em branco — o app pede na hora.",
    "descricao_recorrente": "Texto que vai na nota mensal, descrevendo o serviço.",
    "observacao": "Anotações só para você. Não vai na nota.",
    # ---------------- Emitir (planilha) ----------------
    "competencia": (
        "Mês em que o serviço foi prestado (não o mês do pagamento). "
        "Ex.: trabalho feito em setembro → 09/2026."
    ),
    "sem_pdf": "Marque só se não quiser o PDF da nota. Normalmente deixe desmarcado.",
    "reemitir": (
        "Por segurança, o app não emite duas vezes a mesma linha. Marque só se tiver certeza "
        "de que precisa emitir de novo — pode gerar nota duplicada."
    ),
    "confirmacao": "Uma trava de segurança: digitar a frase confirma que você quer emitir notas de verdade.",
    "confirmacao-recorrencia": "Uma trava de segurança: digitar a frase confirma que você quer emitir a nota de verdade.",
    # ---------------- Importar / listas ----------------
    "planilha": (
        "A planilha do Excel com as notas do mês (.xlsx). Se o Excel salvou em outro "
        "formato, abra e use Arquivo → Salvar como → Pasta de Trabalho do Excel (*.xlsx)."
    ),
    "busca": "Digite parte do nome, do CNPJ/CPF ou do e-mail e clique em Buscar.",
    "historico:busca": "Digite parte do nome do cliente, do CNPJ ou da chave de acesso da nota.",
    "apenas_ativos": "Mostra só os clientes ativos.",
    # ---------------- Nota avulsa ----------------
    "avulsa:documento": "Escolha um cliente já cadastrado. Para alguém novo, marque \"Outro tomador\" acima.",
    "avulsa:competencia": "Mês em que o serviço foi prestado. Ex.: trabalho feito em setembro → 09/2026.",
    "avulsa:servico_ctribnac": (
        "Já vem o código da Configuração. Troque só se esta nota for de outro tipo de serviço — "
        "copie de uma nota desse serviço no portal gov.br (ex.: 17.03.03 → 170303)."
    ),
    "avulsa:servico_ctribmun": (
        "Já vem o da Configuração. Se trocou o código acima, copie também o complementar "
        "da mesma nota do portal (os 3 últimos dígitos, ex.: 001)."
    ),
    "valor": "Valor total do serviço. Digite só os números: 450000 vira 4.500,00.",
    "descricao": "O texto que o cliente vai ler na nota, descrevendo o serviço feito.",
    "novo_documento": "CNPJ ou CPF de quem recebe a nota. Pode digitar com ou sem pontos.",
    "novo_razao_social": "Nome da empresa como está no CNPJ, ou nome completo da pessoa.",
    "novo_email": "Se preencher, a nota é enviada para este e-mail. Em branco, só fica arquivada.",
    "novo_telefone": "Opcional.",
    "novo_logradouro": "Opcional. Se preencher o endereço, informe também número, bairro, cidade e CEP.",
    "novo_numero": "Número do endereço. Sem número, use S/N.",
    "novo_complemento": "Sala, andar, bloco... Opcional.",
    "novo_bairro": "Bairro do endereço.",
    "novo_cod_municipio": "Código IBGE da cidade, 7 dígitos. Rio de Janeiro = 3304557.",
    "novo_uf": "Sigla do estado, ex.: RJ.",
    "novo_cep": "CEP do endereço.",
    "novo_salvar": (
        "Guarda este tomador em Clientes depois de emitir, para não precisar digitar de novo. "
        "Se ele já estiver cadastrado, o cadastro existente não é alterado."
    ),
}
