# -*- coding: utf-8 -*-
"""Conecta Recife - API de home personalizada (v2).

AWS Lambda (Python 3.12) atrás de uma Function URL. Sem pandas: só csv, json e boto3.

Rotas:
  GET /temas
  GET /home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true

Os CSVs são lidos do S3 (variável de ambiente BUCKET, PREFIXO opcional) uma única
vez, na inicialização. As sugestões vêm do modelo pré-calculado (saida/reco_api.json.gz);
o ajuste por faixa etária usa saida/afinidade_faixa.json e o "em alta" usa
saida/em_alta_faixa.json. Tudo fica em variáveis globais.
"""
import csv
import gzip
import io
import json
import os
import unicodedata
from collections import Counter, defaultdict  # noqa: F401
from urllib.parse import unquote_plus

ARQUIVOS = ("servicos", "cidadaos", "historico", "uso_semanal")

MAX_CONTINUE = 3
MAX_SUGESTOES = 10
MAX_EM_ALTA = 5
SEMANAS_EM_ALTA = 4       # janela usada para o "em alta"
AFINIDADE_MIN = 0.5       # abaixo disso (na faixa etária) o serviço vai para o fim da lista
SOBE_INTERESSE = 2        # serviço de tema de interesse sobe no máximo 2 posições
PREFIXO_SAIDA = "saida/"

MARCADORES_SAUDE = ("hipertensao", "diabetes", "saude_mental", "gestante")
COM_CRIANCAS = {"crianca", "adolescente", "primeira_infancia"}

# Estado em memória (preenchido por carregar()).
D = {}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def _bool(valor):
    return str(valor or "").strip().lower() in ("true", "1", "sim", "s", "yes")


def _int(valor):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return 0


def _lista(celula):
    """Células com lista usam "|" como separador."""
    return [p.strip() for p in (celula or "").split("|") if p.strip()]


def _norm(texto):
    """Minúsculas e sem acento, para comparar temas com os interesses."""
    t = unicodedata.normalize("NFKD", (texto or "").strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _data_br(data_iso):
    p = (data_iso or "").split("-")
    return f"{p[2]}/{p[1]}/{p[0]}" if len(p) == 3 else data_iso


def _milhar(n):
    return f"{n:,}".replace(",", ".")


def _faixa_texto(faixa):
    if faixa.endswith("+"):
        return f"de {faixa[:-1]} anos ou mais"
    if "-" in faixa:
        a, b = faixa.split("-", 1)
        return f"de {a} a {b} anos"
    return f"da faixa {faixa}" if faixa else ""


# ----------------------------------------------------------------------------
# Carga e pré-cálculo
# ----------------------------------------------------------------------------
def carregar(abrir, ler_saida):
    """Lê os 4 CSVs e os arquivos de saida/. `abrir(nome)` devolve um arquivo-texto;
    `ler_saida(nome)` devolve os bytes de saida/<nome>."""
    # --- serviços (só as colunas necessárias ficam em memória) ---
    servicos = {}
    temas = set()
    with abrir("servicos") as f:
        for r in csv.DictReader(f):
            sid = (r.get("servico_id") or "").strip()
            titulo = (r.get("nome_resumido") or r.get("nome") or r.get("item_nome") or "").strip()
            if not sid or not titulo:
                continue
            ts = _lista(r.get("temas"))
            temas.update(ts)
            servicos[sid] = {
                "titulo": titulo,
                "temas": ts,
                "temas_norm": [_norm(t) for t in ts],
                "cliques": _int(r.get("cliques_historico")),
            }

    # --- cidadãos ---
    cidadaos = {}
    with abrir("cidadaos") as f:
        for r in csv.DictReader(f):
            cid = (r.get("cidadao_id") or "").strip()
            if not cid:
                continue
            c = {
                "id": cid,
                "idade": _int(r.get("idade")),
                "faixa_etaria": (r.get("faixa_etaria") or "").strip(),
                "rpa": (r.get("rpa") or "").strip(),
                "cadastro_unico": _bool(r.get("cadastro_unico")),
                "empreende": _bool(r.get("mei")) or _bool(r.get("empreendedor")),
                "mei": _bool(r.get("mei")),
                "pessoa_com_deficiencia": _bool(r.get("pessoa_com_deficiencia")),
                "tem_criancas": (r.get("criancas_na_familia") or "").strip().lower() in COM_CRIANCAS,
            }
            for m in MARCADORES_SAUDE:
                c[m] = _bool(r.get(m))
            cidadaos[cid] = c

    # --- histórico: por cidadão, serviço -> último uso ("AAAA-MM-DD HH:MM") ---
    usos = defaultdict(dict)
    with abrir("historico") as f:
        leitor = csv.reader(f)
        cab = next(leitor)
        i_c, i_d, i_h, i_s = (cab.index(n) for n in ("cidadao_id", "data", "hora", "servico_id"))
        for r in leitor:
            sid = r[i_s]
            if sid not in servicos:
                continue
            quando = r[i_d] + " " + r[i_h]
            u = usos[r[i_c]]
            if quando > u.get(sid, ""):
                u[sid] = quando

    # --- em alta: cliques nas semanas mais recentes ---
    por_semana = defaultdict(Counter)
    with abrir("uso_semanal") as f:
        for r in csv.DictReader(f):
            sid = (r.get("servico_id") or "").strip()
            if sid in servicos:
                por_semana[r.get("semana", "")][sid] += _int(r.get("cliques"))
    recentes = sorted(por_semana)[-SEMANAS_EM_ALTA:]
    alta = Counter()
    for s in recentes:
        alta.update(por_semana[s])
    em_alta = sorted(alta.items(), key=lambda kv: (-kv[1], -servicos[kv[0]]["cliques"], kv[0]))

    # Ordem padrão de desempate: mais cliques no histórico primeiro.
    ordem = sorted(servicos, key=lambda s: (-servicos[s]["cliques"], _int(s)))

    # --- saída do modelo (carregada uma vez e mantida em variável global) ---
    bruto = json.loads(gzip.decompress(ler_saida("reco_api.json.gz")).decode("utf-8"))
    afinidade = json.loads(ler_saida("afinidade_faixa.json").decode("utf-8"))
    alta_faixa = json.loads(ler_saida("em_alta_faixa.json").decode("utf-8"))

    D.clear()
    D.update(servicos=servicos, cidadaos=cidadaos, usos=dict(usos),
             em_alta=em_alta[:50], n_semanas=len(recentes),
             ordem=ordem, temas=sorted(temas, key=_norm),
             motivos=bruto["motivos"], reco=bruto["reco"],
             afinidade=afinidade, alta_faixa=alta_faixa)


def carregar_do_s3():
    import boto3  # só é necessário na AWS
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    prefixo = os.environ.get("PREFIXO", "")

    def abrir(nome):
        corpo = s3.get_object(Bucket=bucket, Key=f"{prefixo}{nome}.csv")["Body"].read()
        return io.StringIO(corpo.decode("utf-8-sig"), newline="")

    def ler_saida(nome):
        return s3.get_object(Bucket=bucket, Key=f"{PREFIXO_SAIDA}{nome}")["Body"].read()

    carregar(abrir, ler_saida)


def carregar_da_pasta(pasta, pasta_saida):
    """Para teste local: lê os CSVs de uma pasta e os arquivos de saída de outra, sem S3."""
    def abrir(nome):
        return open(os.path.join(pasta, f"{nome}.csv"), encoding="utf-8-sig", newline="")

    def ler_saida(nome):
        with open(os.path.join(pasta_saida, nome), "rb") as f:
            return f.read()

    carregar(abrir, ler_saida)


# ----------------------------------------------------------------------------
# Montagem da home
# ----------------------------------------------------------------------------
def _resumo(c, consentimento):
    partes = [f"{c['idade']} anos", f"mora na RPA {c['rpa']}" if c["rpa"] else ""]
    if c["empreende"]:
        partes.append("tem negócio próprio (MEI)" if c["mei"] else "tem negócio próprio")
    if c["cadastro_unico"]:
        partes.append("está no CadÚnico")
    if c["pessoa_com_deficiencia"]:
        partes.append("pessoa com deficiência")
    if c["tem_criancas"]:
        partes.append("tem criança ou adolescente na família")
    if consentimento:
        if c["gestante"]:
            partes.append("gestante")
        if c["hipertensao"] or c["diabetes"] or c["saude_mental"]:
            partes.append("acompanha a saúde pela rede municipal")
    return ", ".join(p for p in partes if p) + "."


def _reordenar(itens, faixa, interesses):
    """Ajusta a ordem das sugestões sem remover nenhuma.

    1) afinidade < AFINIDADE_MIN na faixa da pessoa -> vai para o fim (mantém a ordem relativa);
    2) tema em "interesses" -> sobe no máximo SOBE_INTERESSE posições (só entre os que não foram
       para o fim).
    """
    afin = D["afinidade"].get(faixa, {})
    bons = [x for x in itens if afin.get(x["servico_id"], 1.0) >= AFINIDADE_MIN]
    fim = [x for x in itens if afin.get(x["servico_id"], 1.0) < AFINIDADE_MIN]

    def de_interesse(x):
        return any(n in interesses for n in D["servicos"][x["servico_id"]]["temas_norm"])

    # chave: quem é de interesse "finge" estar 2 posições antes; empate favorece o de interesse.
    ordenado = sorted(enumerate(bons), key=lambda ix: (
        ix[0] - SOBE_INTERESSE if de_interesse(ix[1]) else ix[0],
        0 if de_interesse(ix[1]) else 1, ix[0]))
    return [x for _, x in ordenado] + fim


def _sugestoes(cid, c, usados, interesses):
    servicos = D["servicos"]
    itens = []
    for sid, im in D["reco"].get(cid, []):
        sid = str(sid)
        if sid in servicos and sid not in usados:
            itens.append({"servico_id": sid, "motivo": D["motivos"][im]})
    if len(itens) < MAX_SUGESTOES:  # reserva: mais procurados que a pessoa ainda não usou
        tem = {x["servico_id"] for x in itens}
        for sid in D["ordem"]:
            if len(itens) == MAX_SUGESTOES:
                break
            if sid not in usados and sid not in tem:
                itens.append({"servico_id": sid, "motivo": "É um dos serviços mais procurados no Conecta Recife."})
    itens = _reordenar(itens[:MAX_SUGESTOES], c["faixa_etaria"], interesses)

    saida = []
    for posicao, x in enumerate(itens, 1):
        s = servicos[x["servico_id"]]
        tema_int = next((t for t, n in zip(s["temas"], s["temas_norm"]) if n in interesses), None)
        saida.append({
            "posicao": posicao,
            "servico_id": x["servico_id"],
            "titulo": s["titulo"],
            "tema": tema_int or (s["temas"][0] if s["temas"] else ""),
            "motivo": x["motivo"],
        })
    return saida


def montar_home(cid, interesses_txt, consentimento):
    c = D["cidadaos"][cid]
    servicos = D["servicos"]
    usos = D["usos"].get(cid, {})
    interesses = {_norm(t) for t in _lista(interesses_txt)}

    recentes = sorted(usos.items(), key=lambda kv: kv[1], reverse=True)[:MAX_CONTINUE]
    continuar = [{
        "servico_id": sid,
        "titulo": servicos[sid]["titulo"],
        "motivo": f"Você usou este serviço em {_data_br(quando[:10])}.",
    } for sid, quando in recentes]

    sugestoes = _sugestoes(cid, c, usos, interesses)

    # Em alta pela faixa etária da pessoa: não repete o que já aparece nos outros blocos.
    ja = {x["servico_id"] for x in continuar} | {x["servico_id"] for x in sugestoes}
    faixa_txt = _faixa_texto(c["faixa_etaria"])
    em_alta = []
    candidatos = D["alta_faixa"].get(c["faixa_etaria"])
    if candidatos:
        for x in candidatos:
            sid = x["servico_id"]
            if sid in ja or sid not in servicos:
                continue
            if x["anterior"] > 0:
                mais = round(100.0 * (x["recente"] - x["anterior"]) / x["anterior"])
                motivo = f"Em alta entre pessoas {faixa_txt}: {mais}% mais acessos nas últimas 4 semanas."
            else:
                motivo = f"Em alta entre pessoas {faixa_txt}: serviço novo nas últimas 4 semanas."
            em_alta.append({"servico_id": sid, "titulo": servicos[sid]["titulo"], "motivo": motivo})
            if len(em_alta) == MAX_EM_ALTA:
                break
    if len(em_alta) < MAX_EM_ALTA:  # reserva: em alta geral no Recife
        n = D["n_semanas"]
        periodo = "na última semana" if n == 1 else f"nas últimas {n} semanas"
        for sid, cliques in D["em_alta"]:
            if sid in ja or any(e["servico_id"] == sid for e in em_alta):
                continue
            em_alta.append({"servico_id": sid, "titulo": servicos[sid]["titulo"],
                            "motivo": f"Em alta no Recife: {_milhar(cliques)} acessos {periodo}."})
            if len(em_alta) == MAX_EM_ALTA:
                break

    return {
        "cidadao": {
            "id": cid,
            "faixa_etaria": c["faixa_etaria"],
            "rpa": c["rpa"],
            "resumo": _resumo(c, consentimento),
            "modo_simples": c["idade"] >= 60 or c["pessoa_com_deficiencia"],
        },
        "continue": continuar,
        "sugestoes": sugestoes,
        "em_alta": em_alta,
    }


# ----------------------------------------------------------------------------
# Handler
# ----------------------------------------------------------------------------
def _resposta(status, corpo):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(corpo, ensure_ascii=False),
    }


def _param(qs, nome):
    v = (qs.get(nome) or "").strip()
    # A Function URL já entrega decodificado; isto cobre chamadas que chegam cruas.
    return unquote_plus(v) if "%" in v else v


def lambda_handler(event, context):
    try:
        if not D:
            carregar_do_s3()
        event = event or {}
        metodo = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET")
        caminho = (event.get("rawPath") or "/").rstrip("/") or "/"
        qs = event.get("queryStringParameters") or {}

        if metodo not in ("GET", "HEAD"):
            return _resposta(405, {"erro": "Método não permitido. Use GET."})

        if caminho == "/temas":
            return _resposta(200, {"temas": D["temas"]})

        if caminho == "/home":
            cid = _param(qs, "cidadao_id").upper()
            if not cid:
                return _resposta(400, {"erro": "Informe o parâmetro cidadao_id. Exemplo: /home?cidadao_id=C007938"})
            if cid not in D["cidadaos"]:
                return _resposta(404, {"erro": "cidadão não encontrado"})
            return _resposta(200, montar_home(cid, _param(qs, "interesses"),
                                              _bool(_param(qs, "consentimento_saude"))))

        return _resposta(404, {"erro": f"Rota '{caminho}' não existe. Use /temas ou /home."})
    except Exception as e:  # nunca devolver erro que não seja JSON
        print(f"ERRO: {type(e).__name__}: {e}")
        return _resposta(500, {"erro": "Erro interno ao montar a resposta. Tente novamente."})


# Carga na inicialização (cold start) quando estiver na AWS.
if os.environ.get("BUCKET"):
    carregar_do_s3()
