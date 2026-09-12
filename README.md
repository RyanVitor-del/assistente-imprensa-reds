# Assistente de Imprensa — REDS

[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Validate](https://github.com/RyanVitor-del/assistente-imprensa-reds/actions/workflows/validate.yml/badge.svg)](https://github.com/RyanVitor-del/assistente-imprensa-reds/actions/workflows/validate.yml)

Aplicativo desktop para apoiar o fluxo de imprensa a partir de ocorrências do
REDS (Registro de Eventos de Defesa Social). A ferramenta consulta registros,
organiza os resultados, exporta CSV e auxilia na criação de releases e conteúdo
para Instagram.

> Projeto independente e não oficial. O acesso ao REDS exige credenciais
> próprias e autorização institucional. Não use a ferramenta para acessar,
> armazenar ou divulgar dados sem permissão.

## Recursos

- autenticação compatível com o fluxo SSO atual;
- consulta por data, município e unidade responsável;
- reaproveitamento da sessão sem salvar a senha;
- filtro rápido e exportação das ocorrências para CSV;
- exibição do número REDS, natureza, horário e endereço;
- geração assistida de release a partir do PDF;
- apoio à preparação e publicação de conteúdo no Instagram;
- tema claro/escuro e preferências locais.

## Requisitos

- Windows 10 ou 11;
- Firefox atualizado;
- Python 3.13 para executar pelo código-fonte;
- conta autorizada no SISP/REDS.

## Instalação para desenvolvimento

```powershell
git clone https://github.com/RyanVitor-del/assistente-imprensa-reds.git
cd assistente-imprensa-reds

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Copy-Item config.example.json config.json
Copy-Item configmunicipio.example.json configmunicipio.json
Copy-Item configunidade.example.json configunidade.json

python Release.py
```

Preencha em `config.json` somente as integrações que você realmente utilizar.
O login do SISP/REDS é informado na interface e a senha nunca é salva.

## Gerar o executável

```powershell
python -m pip install -r requirements-dev.txt
.\scripts\build.ps1
```

O resultado será criado em `dist\Release-Imprensa.exe`. Os três arquivos
locais de configuração devem permanecer ao lado do executável.

## Estrutura

```text
.
├── .github/workflows/validate.yml
├── assets/
├── scripts/build.ps1
├── Release.py
├── config.example.json
├── configmunicipio.example.json
├── configunidade.example.json
├── requirements.txt
└── SECURITY.md
```

## Privacidade e segurança

Os arquivos reais de configuração, PDFs temporários, executáveis e pacotes são
ignorados pelo Git. Consulte [SECURITY.md](SECURITY.md) antes de distribuir ou
contribuir com o projeto.

## Licença

Distribuído sob a [licença MIT](LICENSE).
