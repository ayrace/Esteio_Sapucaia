# Painel Geográfico de Nodes — Esteio + Sapucaia | V1

Versão inicial para calibração operacional do mapa conjunto de **Esteio + Sapucaia do Sul**.

## Objetivo
A posição exata do node não é o objetivo desta aplicação. O foco é enxergar rapidamente **onde está concentrado o impacto**, usando:

- Cidade
- Região operacional / HV
- Bairro
- Status do node: Online, SS Parcial ou SS Total
- Campo `Impactado` da extração do XPERTrack

As coordenadas da V1 são aproximadas por bairro e servem apenas para visualização de concentração geográfica.

## Regras XPERTrack
- `Pontuação = 0` → porta OFF
- `1 a 20` → porta crítica/degradada, mas ainda online
- `> 20` → porta online
- todas as portas OFF → `SS TOTAL`
- ao menos uma OFF e outra ativa → `SS PARCIAL`
- nenhuma porta OFF → `ONLINE`

## Ajustes de nomenclatura já tratados
- `CPC01/02/03` no XPT → `CCP01/02/03` na topologia
- `PIR04A1-1` e `PIR04A2-1` → node lógico `PIR04`, portas 1 e 2
- `EIO42-1` → mantido no painel como `EIO42`, ainda sem cadastro de topologia/HV/bairro validado

## Atualização da coleta
A V1 abre com `data/ESTEIO_SAPUCAIA.csv` incluído no pacote.

Também há um uploader dentro do painel para testar uma nova extração sem alterar os arquivos.

Para atualização automática pelo Google Drive, configure nos **Secrets** do Streamlit:

```toml
DRIVE_FOLDER_ID = "ID_DA_PASTA_PUBLICA"
DRIVE_FILE_NAME = "ESTEIO_SAPUCAIA.csv"
```

Opcionalmente, se preferir apontar diretamente para um arquivo:

```toml
DRIVE_FILE_ID = "ID_DO_ARQUIVO"
```

## Arquivos principais
- `streamlit_app.py` — aplicação
- `base_nodes_esteio_sapucaia.csv` — base geográfica operacional
- `data/ESTEIO_SAPUCAIA.csv` — coleta XPT de teste
- `data/correcoes_geograficas.csv` — tabela simples para revisão de cidade/região/bairro
- `referencia_topologia.json` — topologia recebida

## Implantação no Streamlit
1. Envie os arquivos para um repositório GitHub.
2. No Streamlit Community Cloud, selecione `streamlit_app.py`.
3. A V1 já funciona com o CSV incluído.
4. Após validar bairros/regiões, configure o Drive para a atualização automática.

## O que revisar na V1
Abra o expander **“Base V1 — bairros/regiões para validar”**. Ele mostra os nodes em que o bairro foi inferido e ainda deve ser conferido visualmente/operacionalmente.
