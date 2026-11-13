# Conecta Recife — API de home personalizada

API do hackathon Conecta Recife que monta a tela inicial de cada pessoa: serviços para continuar,
sugestões personalizadas com o motivo de cada uma, serviços em alta e a próxima ação recomendada.
Também abre e acompanha solicitações de serviço por protocolo.

## Visão geral

| Arquivo | O que é |
|---|---|
| `lambda_function.py` | A API. Roda em AWS Lambda (Python 3.12) atrás de uma Function URL, sem pandas — só biblioteca padrão e `boto3`. |
| `modelo/recomendar.py` | O modelo de recomendação. Roda fora da Lambda e gera os arquivos que a API lê. |
| `testar_local.py` | Teste das rotas no computador, lendo os arquivos do disco (sem S3). |

Fluxo dos dados:

1. `modelo/recomendar.py` lê os CSVs, valida o modelo e grava as recomendações em `saida/` no S3.
2. Na inicialização (uma única vez), a Lambda lê do bucket indicado na variável de ambiente `BUCKET`:
   - `servicos.csv`, `cidadaos.csv`, `historico.csv` e `uso_semanal.csv`;
   - `saida/reco_api.json.gz` — sugestões por cidadão, cada uma com seu motivo;
   - `saida/afinidade_faixa.json` — afinidade de cada serviço por faixa etária;
   - `saida/em_alta_faixa.json` — serviços em alta por faixa etária.
3. Tudo fica em memória; cada requisição só consulta e ordena, sem acessar o S3.

**Dados fora do repositório:** os CSVs e os arquivos de `saida/` não são versionados (veja `.gitignore`).

## Rotas

Todas as respostas são JSON em UTF-8 (`Content-Type: application/json; charset=utf-8`), inclusive os erros.

| Rota | Para que serve |
|---|---|
| `GET /temas` | Lista dos temas dos serviços |
| `GET /home?cidadao_id=...` | Home personalizada de um cidadão |
| `GET /home` (sem `cidadao_id`) | Home anônima, para visitante |
| `POST /solicitacoes` | Abre uma solicitação e devolve o protocolo |
| `GET /solicitacoes/{protocolo}?servico_id=...` | Acompanha uma solicitação |

### `GET /temas`

Lista única e ordenada dos temas dos serviços.

```json
{"temas": ["Administração Pública", "Animais", "Cidadania", "..."]}
```

### `GET /home`

| Parâmetro | Obrigatório | Descrição |
|---|---|---|
| `cidadao_id` | não | Identificador do cidadão, ex.: `C007938`. Sem ele, a resposta é a home anônima. |
| `interesses` | não | Temas de interesse (veja abaixo). |
| `consentimento_saude` | não | `true` para permitir que o resumo do perfil cite marcadores de saúde (padrão: `false`). |

`interesses` aceita os nomes devolvidos por `/temas` ou os ids abaixo, separados por `|` ou `,`,
ou como lista JSON (`["saude","emprego"]`):

| id | Tema |
|---|---|
| `saude` | Saúde |
| `beneficios` | Cidadania |
| `esporte` | Esportes e Bem-Estar |
| `emprego` | Trabalho/Qualificação |
| `estudos` | Educação |
| `bichinho` | Animais |
| `casa` | Imóveis |
| `cultura` | Cultura e Lazer |
| `rua` | Serviços Urbanos |

Exemplo: `/home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true`

Formato da resposta (os textos abaixo são ilustrativos):

```json
{
  "cidadao": {
    "id": "C007938",
    "faixa_etaria": "60-74",
    "rpa": "3",
    "resumo": "68 anos, mora na RPA 3.",
    "modo_simples": true,
    "tem_historico": true
  },
  "continue": [
    {"servico_id": "519", "titulo": "Ver resultado de exames laboratoriais",
     "motivo": "Você usou este serviço em 11/09/2026."}
  ],
  "sugestoes": [
    {"posicao": 1, "servico_id": "942",
     "titulo": "Agendar consultas e procedimentos na Minha Unidade de Saúde",
     "tema": "Saúde",
     "motivo": "Quem usou Ver resultado de exames também costuma usar este"}
  ],
  "em_alta": [
    {"servico_id": "858", "titulo": "Realizar agendamento para a Academia Recife",
     "motivo": "Em alta entre pessoas de 60 a 74 anos: 35% mais acessos nas últimas 4 semanas."}
  ],
  "proxima_acao": {
    "servico_id": "942",
    "titulo": "Agendar consultas e procedimentos na Minha Unidade de Saúde",
    "frase": "Quem usa Ver resultado de exames costuma precisar disto."
  }
}
```

- `continue`: até 3 serviços usados mais recentemente.
- `sugestoes`: até 10, sem repetir serviços já usados.
- `em_alta`: até 5, sem repetir o que já está nos outros blocos.
- `proxima_acao`: a primeira sugestão, com uma frase de até 12 palavras.
- `modo_simples`: `true` se idade >= 60 ou pessoa com deficiência.
- `tem_historico`: `true` se a pessoa tem algum uso registrado.

**Home anônima** (sem `cidadao_id`): mesmo formato, com `cidadao` igual a
`{"id": null, "faixa_etaria": null, "resumo": "Visitante", "modo_simples": false}` e `continue` vazio.
Se houver `interesses`, as posições 1 e 2 trazem serviços dos temas escolhidos; o restante são os
serviços mais procurados no Recife.

### `POST /solicitacoes`

Corpo em JSON:

```json
{
  "cidadao_id": "C007938",
  "servico_id": "199",
  "campos": {
    "tipo": "Buraco na via",
    "endereco": "Rua da Aurora, 100",
    "referencia": "Em frente à praça",
    "descricao": "Buraco grande na faixa da direita."
  }
}
```

`cidadao_id`, `servico_id`, `tipo`, `endereco` e `descricao` são obrigatórios; `referencia` é opcional.
Resposta `201`:

```json
{
  "protocolo": "CR-2026-0ABC12",
  "servico": "Título do serviço",
  "data_abertura": "12/11/2026 22:30",
  "prazo_estimado": "até 5 dias úteis",
  "proximos_passos": ["Sua solicitação foi registrada e já está na fila.", "..."],
  "linha_do_tempo": [
    {"etapa": "Recebido", "status": "atual"},
    {"etapa": "Em análise", "status": "pendente"},
    {"etapa": "Equipe a caminho", "status": "pendente"},
    {"etapa": "Concluído", "status": "pendente"}
  ]
}
```

### `GET /solicitacoes/{protocolo}?servico_id=...`

Devolve o mesmo formato, com a linha do tempo atualizada.

**Limite desta versão (demonstração):** as solicitações não são armazenadas. O protocolo carrega o
horário de abertura e as etapas avançam conforme o tempo passa (1, 3 e 5 minutos), para mostrar o
acompanhamento na tela. O conteúdo de `campos` é validado, mas não é guardado.

### Erros

| Status | Quando |
|---|---|
| `400` | Corpo inválido ou campo obrigatório faltando em `/solicitacoes`; `servico_id` ausente na consulta |
| `404` | `cidadao_id`, serviço, protocolo ou rota inexistente |
| `405` | Método não permitido para a rota |
| `500` | Erro interno |

```json
{"erro": "cidadão não encontrado"}
```

## Como as sugestões são montadas

1. **Origem:** as sugestões e seus motivos vêm prontos do modelo (`saida/reco_api.json.gz`).
   Serviços que a pessoa já usou são retirados; se sobrarem menos de 10, a lista é completada com
   os serviços mais procurados que ela ainda não usou.
2. **Ajuste por faixa etária:** serviço com afinidade abaixo de 0,5 na faixa etária da pessoa
   (`saida/afinidade_faixa.json`) vai para o fim da lista.
3. **Interesses:** serviço de um tema informado em `interesses` sobe no máximo 2 posições.
4. **Diversidade de temas:** no máximo 3 serviços do mesmo tema nas posições 1 a 6 e no máximo 5
   nas posições 1 a 10.
5. **Tema mais usado:** se um tema concentra 40% ou mais dos usos da pessoa, o melhor serviço desse
   tema entra nas 3 primeiras posições, com o motivo "Combina com o que você mais usa".
6. **Em alta por faixa etária:** vem de `saida/em_alta_faixa.json`, com o crescimento de acessos nas
   últimas 4 semanas entre pessoas da mesma faixa. Se faltar item, completa com os mais acessados do
   Recife no mesmo período (`uso_semanal.csv`).

## Privacidade

- Nenhum dado de cidadão está no repositório: CSVs, JSONs e a pasta `saida/` são ignorados pelo git.
- Marcadores de saúde (hipertensão, diabetes, saúde mental, gestante) só aparecem no resumo do
  perfil com `consentimento_saude=true`, e mesmo assim sem citar a condição.
- Os motivos das sugestões falam do uso dos serviços, não de características pessoais.

## Modelo

`modelo/recomendar.py` gera os arquivos de `saida/`. Roda fora da Lambda (no Kiro), com pandas,
numpy e scipy. Comandos:

| Comando | O que faz |
|---|---|
| `validar` | Validação offline: treina com o histórico até 28 dias antes da última data e mede hit rate@10 e MRR@10 nos 28 dias finais |
| `gerar` | Gera as 10 recomendações de cada cidadão, com o motivo de cada uma |
| `checar` | Confere o arquivo gerado (colunas, motivos vazios) e mostra exemplos |
| `aux_api` | Gera `afinidade_faixa.json` e `em_alta_faixa.json` |

## Teste local

Coloque os quatro CSVs na pasta `dados\` (ou na mesma pasta dos scripts) e, na pasta `saida\` ao lado
do script, os arquivos `reco_api.json.gz`, `afinidade_faixa.json` e `em_alta_faixa.json` baixados de
`s3://claudinhas-dados-hack/saida/`. Depois rode:

```
py testar_local.py
```

O teste não usa S3. Ele chama `/temas`, `/home` para C007938, C068579 e C076260, a home anônima,
a abertura e a consulta de uma solicitação e os casos de erro.

## Publicação na AWS

- Runtime: Python 3.12, handler `lambda_function.lambda_handler`.
- Variável de ambiente `BUCKET` com o nome do bucket; `PREFIXO` é opcional, para os CSVs.
- A função precisa de permissão de leitura (`s3:GetObject`) no bucket.
