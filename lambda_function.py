# -*- coding: utf-8 -*-
"""Conecta Recife - API de home personalizada (v1).

AWS Lambda (Python 3.12) atrás de uma Function URL. Sem pandas: só csv, json e boto3.

Rotas:
  GET /temas
  GET /home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true

Os CSVs são lidos do S3 (variável de ambiente BUCKET, PREFIXO opcional) uma única
vez, na inicialização, e todos os agrupamentos ficam pré-calculados em memória.
"""
import csv
import io
import json
import os
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote_plus

ARQUIVOS = ("servicos", "cidadaos", "historico", "uso_semanal")

MAX_CONTINUE = 3
MAX_SUGESTOES = 10
MAX_EM_ALTA = 5
SEMANAS_EM_ALTA = 4       # janela usada para o "em alta"
MIN_GRUPO = 30            # grupo menor que isso cai para um grupo mais amplo
BONUS_INTERESSE_MULT = 1.5
BONUS_INTERESSE_SOMA = 5.0
BONUS_SAUDE_MULT = 1.2
BONUS_SAUDE_SOMA = 2.0
TEMA_SAUDE = "saude"

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
def _chave(c, com_gestante):
    """Chave de "cidadãos parecidos": faixa etária + marcadores principais."""
    base = (c["faixa_etaria"], c["cadastro_unico"], c["empreende"],
            c["pessoa_com_deficiencia"], c["tem_criancas"])
    return base + (c["gestante"],) if com_gestante else base


def carregar(abrir):
    """Lê os 4 CSVs e pré-calcula tudo. `abrir(nome)` devolve um arquivo-texto."""
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

    # --- popularidade por grupo (nº de cidadãos distintos que usaram cada serviço) ---
    # Níveis: perfil completo com gestante (só com consentimento), perfil sem
    # gestante, só faixa etária e geral. Grupo pequeno cai para o nível seguinte.
    tam = Counter()
    pop = defaultdict(Counter)
    for cid, c in cidadaos.items():
        chaves = (("g", _chave(c, True)), ("p", _chave(c, False)),
                  ("f", c["faixa_etaria"]), ("t",))
        usados = list(usos.get(cid, ()))
        for k in chaves:
            tam[k] += 1
            if usados:
                pop[k].update(usados)

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

    D.clear()
    D.update(servicos=servicos, cidadaos=cidadaos, usos=dict(usos), tam=tam,
             pop=dict(pop), em_alta=em_alta[:50], n_semanas=len(recentes),
             ordem=ordem, temas=sorted(temas, key=_norm))


def carregar_do_s3():
    import boto3  # só é necessário na AWS
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    prefixo = os.environ.get("PREFIXO", "")

    def abrir(nome):
        corpo = s3.get_object(Bucket=bucket, Key=f"{prefixo}{nome}.csv")["Body"].read()
        return io.StringIO(corpo.decode("utf-8-sig"), newline="")

    carregar(abrir)


def carregar_da_pasta(pasta):
    """Para teste local: lê os CSVs de uma pasta, sem S3."""
    def abrir(nome):
        return open(os.path.join(pasta, f"{nome}.csv"), encoding="utf-8-sig", newline="")

    carregar(abrir)


# ----------------------------------------------------------------------------
# Montagem da home
# ----------------------------------------------------------------------------
def _grupo(c, consentimento):
    """Escolhe o grupo de parecidos mais específico que tenha gente suficiente."""
    niveis = []
    if consentimento:
        niveis.append((("g", _chave(c, True)), "perfil"))
    niveis.append((("p", _chave(c, False)), "perfil"))
    niveis.append((("f", c["faixa_etaria"]), "faixa"))
    for chave, nivel in niveis:
        if D["tam"].get(chave, 0) >= MIN_GRUPO:
            return chave, nivel
    return ("t",), "geral"


def _descricao_grupo(c, nivel, consentimento):
    if nivel == "geral":
        return "quem usa o Conecta Recife"
    texto = "pessoas " + _faixa_texto(c["faixa_etaria"])
    if nivel == "perfil":
        texto += " com perfil parecido com o seu"
    return texto


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


def _sugestoes(c, usados, interesses, consentimento):
    servicos = D["servicos"]
    chave, nivel = _grupo(c, consentimento)
    contagem = D["pop"].get(chave, {})
    tamanho = max(D["tam"].get(chave, 0), 1)
    grupo_txt = _descricao_grupo(c, nivel, consentimento)
    # Marcadores de saúde só entram com consentimento.
    foco_saude = consentimento and any(c[m] for m in MARCADORES_SAUDE)

    candidatos = []
    for pos_cliques, sid in enumerate(D["ordem"]):
        if sid in usados:
            continue
        s = servicos[sid]
        pct = 100.0 * contagem.get(sid, 0) / tamanho
        tema_int = next((t for t, n in zip(s["temas"], s["temas_norm"]) if n in interesses), None)
        saude = foco_saude and TEMA_SAUDE in s["temas_norm"]
        pontos = pct
        if tema_int:
            pontos = pontos * BONUS_INTERESSE_MULT + BONUS_INTERESSE_SOMA
        if saude:
            pontos = pontos * BONUS_SAUDE_MULT + BONUS_SAUDE_SOMA
        # desempate: cliques_historico (D["ordem"] já está nessa ordem)
        candidatos.append((-pontos, pos_cliques, sid, pct, tema_int, saude))
    candidatos.sort()

    saida = []
    for posicao, (_, _, sid, pct, tema_int, saude) in enumerate(candidatos[:MAX_SUGESTOES], 1):
        s = servicos[sid]
        if pct >= 1:
            uso = f"{round(pct)} em cada 100 {grupo_txt} já usaram"
        elif pct > 0:
            uso = f"{grupo_txt} também usam"
        else:
            uso = ""
        if tema_int and uso:
            motivo = f"Combina com seu interesse em {tema_int}. " + uso[0].upper() + uso[1:] + "."
        elif tema_int:
            motivo = f"Combina com seu interesse em {tema_int}."
        elif saude and uso:
            motivo = "Serviço de saúde que pode ajudar você. " + uso[0].upper() + uso[1:] + "."
        elif saude:
            motivo = "Serviço de saúde que pode ajudar você."
        elif uso:
            motivo = uso[0].upper() + uso[1:] + "."
        else:
            motivo = "É um dos serviços mais procurados no Conecta Recife."
        saida.append({
            "posicao": posicao,
            "servico_id": sid,
            "titulo": s["titulo"],
            "tema": tema_int or (s["temas"][0] if s["temas"] else ""),
            "motivo": motivo,
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

    sugestoes = _sugestoes(c, usos, interesses, consentimento)

    # Em alta: não repete o que já aparece nos outros blocos da home.
    ja = {x["servico_id"] for x in continuar} | {x["servico_id"] for x in sugestoes}
    n = D["n_semanas"]
    periodo = "na última semana" if n == 1 else f"nas últimas {n} semanas"
    em_alta = []
    for sid, cliques in D["em_alta"]:
        if sid in ja:
            continue
        em_alta.append({
            "servico_id": sid,
            "titulo": servicos[sid]["titulo"],
            "motivo": f"Em alta no Recife: {_milhar(cliques)} acessos {periodo}.",
        })
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
                return _resposta(404, {"erro": f"Cidadão '{cid}' não encontrado. Confira o cidadao_id informado."})
            return _resposta(200, montar_home(cid, _param(qs, "interesses"),
                                              _bool(_param(qs, "consentimento_saude"))))

        return _resposta(404, {"erro": f"Rota '{caminho}' não existe. Use /temas ou /home."})
    except Exception as e:  # nunca devolver erro que não seja JSON
        print(f"ERRO: {type(e).__name__}: {e}")
        return _resposta(500, {"erro": "Erro interno ao montar a resposta. Tente novamente."})


# Carga na inicialização (cold start) quando estiver na AWS.
if os.environ.get("BUCKET"):
    carregar_do_s3()
