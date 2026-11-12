# Conecta Recife — API de home personalizada (v2)

API do hackathon Conecta Recife que monta a tela inicial de cada cidadão: serviços para
continuar, sugestões personalizadas e serviços em alta.

Roda em AWS Lambda (Python 3.12) atrás de uma Function URL, sem pandas — só `csv`, `json` e `boto3`.
Na inicialização lê do bucket S3 indicado na variável de ambiente `BUCKET`, uma única vez:

- `servicos.csv`, `cidadaos.csv`, `historico.csv` e `uso_semanal.csv`;
- a saída do modelo, em `saida/`: `reco_api.json.gz` (sugestões por cidadão, com o motivo),
  `afinidade_faixa.json` (afinidade de cada serviço por faixa etária) e `em_alta_faixa.json`
  (serviços em alta por faixa etária).

Os CSVs e os arquivos de `saida/` **não** fazem parte deste repositório (veja `.gitignore`).

## Rotas

### `GET /temas`

Lista única e ordenada dos temas dos serviços.

```json
{"temas": ["Administração Pública", "Animais", "Cidadania", "..."]}
```

### `GET /home`

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `cidadao_id` | sim | Identificador do cidadão, ex.: `C007938` |
| `interesses` | não | Temas de interesse separados por `\|`, com os nomes devolvidos por `/temas`, ex.: `Saúde\|Educação` |
| `consentimento_saude` | não | `true` para permitir que o resumo do perfil cite marcadores de saúde (padrão: `false`) |

Exemplo: `/home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true`

Formato da resposta (os textos de `motivo` abaixo são ilustrativos; os das sugestões vêm do modelo):

```json
{
  "cidadao": {
    "id": "C007938",
    "faixa_etaria": "60-74",
    "rpa": "3",
    "resumo": "68 anos, mora na RPA 3.",
    "modo_simples": true
  },
  "continue": [
    {"servico_id": "519", "titulo": "Ver resultado de exames laboratoriais",
     "motivo": "Você usou este serviço em 11/09/2026."}
  ],
  "sugestoes": [
    {"posicao": 1, "servico_id": "942",
     "titulo": "Agendar consultas e procedimentos na Minha Unidade de Saúde",
     "tema": "Saúde",
     "motivo": "Muito usado por pessoas com perfil parecido com o seu."}
  ],
  "em_alta": [
    {"servico_id": "858", "titulo": "Realizar agendamento para a Academia Recife",
     "motivo": "Em alta entre pessoas de 60 a 74 anos: 35% mais acessos nas últimas 4 semanas."}
  ]
}
```

- `continue`: até 3 serviços usados mais recentemente.
- `sugestoes`: até 10, sem repetir serviços já usados.
- `em_alta`: até 5, sem repetir o que já está nos outros blocos.
- `modo_simples`: `true` se idade >= 60 ou pessoa com deficiência.

Erros também vêm em JSON: `404` para `cidadao_id` inexistente ou rota desconhecida, `400` sem `cidadao_id`.

```json
{"erro": "cidadão não encontrado"}
```

## Regras das sugestões (v2)

1. **Origem:** as sugestões e seus motivos vêm prontos do modelo, em `saida/reco_api.json.gz` no S3.
   Serviços que a pessoa já usou são retirados; se sobrarem menos de 10, a lista é completada com
   os serviços mais procurados que ela ainda não usou.
2. **Ajuste por faixa etária:** serviço com afinidade abaixo de 0,5 na faixa etária da pessoa
   (`saida/afinidade_faixa.json`) vai para o fim da lista. Nenhum é removido.
3. **Interesses:** serviço de um tema informado em `interesses` sobe no máximo 2 posições.
   Os temas devem usar os nomes devolvidos por `/temas`.
4. **Em alta por faixa etária:** vem de `saida/em_alta_faixa.json`, com o crescimento de acessos
   nas últimas 4 semanas entre pessoas da mesma faixa. Se faltar item, completa com os mais
   acessados do Recife no mesmo período (`uso_semanal.csv`).

## Modelo

`modelo\recomendar.py` gera os arquivos de `saida/`. Comandos: `validar`, `gerar`, `checar` e
`aux_api`. Roda no Kiro, com pandas, numpy e scipy (não faz parte da Lambda).

## Teste local

Coloque os quatro CSVs na pasta `dados\` (ou na mesma pasta dos scripts) e, na pasta `saida\` ao lado
do script, os arquivos `reco_api.json.gz`, `afinidade_faixa.json` e `em_alta_faixa.json` baixados de
`s3://claudinhas-dados-hack/saida/`. Depois rode:

```
py testar_local.py
```

O teste não usa S3: chama `/temas`, `/home` para C007938, C068579 e C076260 e os casos de erro.
