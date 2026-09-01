# Como gerar o instalador Windows

> **Este passo precisa de uma máquina Windows.** O PyInstaller não faz
> compilação cruzada: um executável Windows só é gerado no Windows.

## 1. Preparar o ambiente

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
```

## 2. Gerar o aplicativo

```bat
pyinstaller empacotamento\emissor.spec --noconfirm
```

Sai em `dist\EmissorNFSe\`. Teste antes de empacotar:

```bat
dist\EmissorNFSe\EmissorNFSe.exe
```

O navegador deve abrir sozinho no painel do emissor.

## 3. Gerar o instalador

Instale o [Inno Setup 6](https://jrsoftware.org/isdl.php), abra
`empacotamento\instalador.iss` no Inno Setup Compiler e clique em **Compile**.
O instalador sai em `empacotamento\saida\`.

## 4. Testar numa máquina limpa

Instale **num computador sem Python** e confira:

- [ ] O atalho do Menu Iniciar abre o navegador no painel
- [ ] A tela de Configuração salva o certificado e a senha
- [ ] O botão "Testar certificado" mostra titular e validade
- [ ] Uma simulação gera o XML na pasta de notas
- [ ] A pasta de backup recebe os arquivos
- [ ] Fechar a janela do console encerra o aplicativo

---

## Coisas que vão aparecer

**Antivírus acusando o executável.** Falso-positivo com binário PyInstaller é
comum — o padrão de um interpretador embutido parece com o de empacotadores de
malware. Por isso o `.spec` usa one-folder e desliga o UPX, que são os dois
maiores agravantes. Se o antivírus corporativo ainda reclamar, peça ao TI uma
exceção para a pasta de instalação. Assinar o executável com um certificado de
code signing resolve de vez, mas é um custo à parte.

**Aviso do SmartScreen na primeira execução.** Some sozinho conforme o
instalador ganha reputação, ou some na hora se o executável for assinado.

**Tamanho.** Cerca de 80–120 MB, porque pandas, lxml e cryptography vão junto.
É o preço de não exigir Python instalado na máquina do usuário.

---

## Instalando em mais de um computador

`nDPS` é único **por série**. Cada laptop precisa de uma **série própria**:

| Computador | Série |
|---|---|
| Laptop 1 | 1 |
| Laptop 2 | 2 |
| Laptop 3 | 3 |

Defina isso na tela de Configuração logo depois de instalar. O aplicativo grava
o nome da máquina no arquivo de controle e **bloqueia a emissão** se detectar
que o controle veio de outro computador — o que acontece, por exemplo, ao
restaurar um backup na máquina errada.

O cadastro de clientes não é compartilhado automaticamente. Use
**Clientes → Exportar cadastro** numa máquina e **Importar** na outra. É manual
de propósito: colocar o banco SQLite numa pasta do OneDrive ou do Drive corrompe
o arquivo, porque a sincronização copia banco aberto.

## Atualizando

Gere a nova versão e rode o instalador por cima. A configuração, o cadastro e o
controle de numeração ficam em `%APPDATA%\EmissorNFSe` e **não são tocados** —
nem pela atualização, nem pela desinstalação.
