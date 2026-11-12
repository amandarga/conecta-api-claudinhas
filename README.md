# Conecta Recife — API de home personalizada (v1)

API do hackathon Conecta Recife que monta a tela inicial de cada cidadão: serviços para
continuar, sugestões personalizadas e serviços em alta.

Roda em AWS Lambda (Python 3.12) atrás de uma Function URL, sem pandas — só `csv`, `json` e `boto3`.
Na inicialização lê `servicos.csv`, `cidadaos.csv`, `historico.csv` e `uso_semanal.csv` do bucket S3
indicado na variável de ambiente `BUCKET` e deixa os agrupamentos pré-calculados em memória.

Os CSVs **não** fazem parte deste repositório (veja `.gitignore`).

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
| `interesses` | não | Temas de interesse separados por `\|`, ex.: `Saúde\|Educação` |
| `consentimento_saude` | não | `true` para permitir o uso dos marcadores de saúde (padrão: `false`) |

Exemplo: `/home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true`

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
     "motivo": "Combina com seu interesse em Saúde. 26 em cada 100 pessoas de 60 a 74 anos com perfil parecido com o seu já usaram."}
  ],
  "em_alta": [
    {"servico_id": "858", "titulo": "Realizar agendamento para a Academia Recife",
     "motivo": "Em alta no Recife: 16.862 acessos nas últimas 4 semanas."}
  ]
}
```

- `continue`: até 3 serviços usados mais recentemente.
- `sugestoes`: até 10, sem repetir serviços já usados.
- `em_alta`: até 5, pelos cliques das 4 semanas mais recentes.
- `modo_simples`: `true` se idade >= 60 ou pessoa com deficiência.

Erros também vêm em JSON: `404` para `cidadao_id` inexistente ou rota desconhecida, `400` sem `cidadao_id`.

```json
{"erro": "Cidadão 'C999999' não encontrado. Confira o cidadao_id informado."}
```

## Regras das sugestões (v1)

1. Popularidade entre cidadãos parecidos: mesma faixa etária e mesmos marcadores principais
   (CadÚnico, MEI ou empreendedor, pessoa com deficiência, crianças na família, gestante).
2. Bônus para serviços dos temas informados em `interesses`.
3. Desempate por `cliques_historico`.
4. Marcadores de saúde (hipertensão, diabetes, saúde mental, gestante) só são usados com
   `consentimento_saude=true`.

## Teste local

Coloque os quatro CSVs na pasta `dados\` (ou na mesma pasta dos scripts) e rode:

```
py testar_local.py
```

O teste não usa S3: chama `/temas`, `/home` para C007938, C068579 e C076260 e os casos de erro.
