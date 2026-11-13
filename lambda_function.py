# -*- coding: utf-8 -*-
"""Conecta Recife - API de home personalizada (v2).

AWS Lambda (Python 3.12) atrás de uma Function URL. Sem pandas: só csv, json e boto3.

Rotas:
  GET /temas
  POST /solicitacoes   {cidadao_id, servico_id, campos:{tipo, endereco, referencia, descricao}}
  GET /solicitacoes/{protocolo}?servico_id=...
  GET /home            (sem cidadao_id: home anônima, com ?interesses= opcional)
  GET /home?cidadao_id=C007938&interesses=Saúde|Educação&consentimento_saude=true
    interesses: nomes de /temas ou ids (saude, beneficios, esporte, emprego, estudos,
    bichinho, casa, cultura, rua), separados por "|" ou ",", ou lista JSON.

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
import re
import time
import unicodedata
import base64
from collections import Counter, defaultdict  # noqa: F401
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

ARQUIVOS = ("servicos", "cidadaos", "historico", "uso_semanal")

MAX_CONTINUE = 3
MAX_SUGESTOES = 10
MAX_TEMA_TOP6 = 3         # no máximo 3 itens do mesmo tema nas posições 1-6
MAX_TEMA_TOP10 = 5        # e no máximo 5 nas posições 1-10
DOMINANTE_MIN = 0.40      # tema primário com >= 40% dos usos do histórico
MAX_EM_ALTA = 5
SEMANAS_EM_ALTA = 4       # janela usada para o "em alta"
AFINIDADE_MIN = 0.5       # abaixo disso (na faixa etária) o serviço vai para o fim da lista
SOBE_INTERESSE = 2        # serviço de tema de interesse sobe no máximo 2 posições
PREFIXO_SAIDA = "saida/"

MARCADORES_SAUDE = ("hipertensao", "diabetes", "saude_mental", "gestante")
COM_CRIANCAS = {"crianca", "adolescente", "primeira_infancia"}

# ids de interesse aceitos em /home, além dos nomes de /temas.
IDS_INTERESSE = {
    "saude": "Saúde",
    "beneficios": "Cidadania",
    "esporte": "Esportes e Bem-Estar",
    "emprego": "Trabalho/Qualificação",
    "estudos": "Educação",
    "bichinho": "Animais",
    "casa": "Imóveis",
    "cultura": "Cultura e Lazer",
    "rua": "Serviços Urbanos",
}

# Nome amigável de cada id, usado no motivo da home anônima ("Você escolheu o assunto ...").
NOMES_AMIGAVEIS = {
    "saude": "Saúde e consultas",
    "beneficios": "Benefícios e documentos",
    "esporte": "Esporte e bem-estar",
    "emprego": "Emprego e cursos",
    "estudos": "Estudos e escola",
    "bichinho": "Meu bichinho",
    "casa": "Casa e IPTU",
    "cultura": "Cultura e eventos",
    "rua": "Rua e bairro",
}

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


def _interesses_itens(texto):
    """Interpreta o parâmetro `interesses`: lista JSON, ou texto separado por "|" ou ",".

    Aceita ids (IDS_INTERESSE) e nomes de tema; devolve a lista (na ordem dada, sem repetição) de (tema normalizado, id usado ou None).
    Nomes de tema que contêm vírgula (ex.: "Licenças, Alvarás e Permissões") são reconhecidos
    juntando pedaços consecutivos.
    """
    texto = (texto or "").strip()
    if not texto:
        return []
    itens = None
    if texto.startswith("["):
        try:
            v = json.loads(texto)
            if isinstance(v, list):
                itens = [str(x) for x in v]
        except ValueError:
            pass
    if itens is None:
        itens = texto.split("|")
    conhecidos = {_norm(t) for t in D.get("temas", [])}
    resultado = []
    for item in itens:
        pedacos = [p.strip() for p in item.split(",") if p.strip()]
        i = 0
        while i < len(pedacos):
            for j in range(len(pedacos), i, -1):  # tenta o trecho mais longo que seja um tema/id
                cand = _norm(", ".join(pedacos[i:j]))
                if cand in IDS_INTERESSE or cand in conhecidos:
                    break
            else:
                j = i + 1
                cand = _norm(pedacos[i])
            t = _norm(IDS_INTERESSE[cand]) if cand in IDS_INTERESSE else cand
            if all(t != r[0] for r in resultado):
                resultado.append((t, cand if cand in IDS_INTERESSE else None))
            i = j
    return resultado


def _interesses(texto):
    """Só os temas normalizados (ordem dada, sem repetição)."""
    return [t for t, _ in _interesses_itens(texto)]


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
    com_historico = set()  # cidadãos com ao menos 1 linha em historico.csv
    por_tema = defaultdict(Counter)  # cidadão -> tema primário -> nº de usos
    with abrir("historico") as f:
        leitor = csv.reader(f)
        cab = next(leitor)
        i_c, i_d, i_h, i_s = (cab.index(n) for n in ("cidadao_id", "data", "hora", "servico_id"))
        for r in leitor:
            com_historico.add(r[i_c])
            sid = r[i_s]
            if sid not in servicos:
                continue
            if servicos[sid]["temas"]:
                por_tema[r[i_c]][servicos[sid]["temas"][0]] += 1
            quando = r[i_d] + " " + r[i_h]
            u = usos[r[i_c]]
            if quando > u.get(sid, ""):
                u[sid] = quando

    dominante = {}
    for cid_, cont in por_tema.items():
        t = _dominante(cont)
        if t:
            dominante[cid_] = t
    del por_tema

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
    D.update(servicos=servicos, cidadaos=cidadaos, usos=dict(usos), com_historico=com_historico, dominante=dominante,
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


# ----------------------------------------------------------------------------
# Ordenação final: diversidade de temas e afinidade com o histórico
# ----------------------------------------------------------------------------
def _nome_tema(tema):
    """Nome amigável do tema (o mesmo dos ids de interesse); sem id, o próprio nome do tema."""
    for id_, t in IDS_INTERESSE.items():
        if t == tema:
            return NOMES_AMIGAVEIS.get(id_, tema)
    return tema


def _dominante(contagem):
    """Tema primário com mais usos, se tiver pelo menos DOMINANTE_MIN dos usos; senão None.
    Empate: vale o nome em ordem alfabética."""
    total = sum(contagem.values())
    if not total:
        return None
    n, tema = min((-v, t) for t, v in contagem.items())
    return tema if -n / total >= DOMINANTE_MIN else None


def _completar(final, pool, tema_de, n=MAX_SUGESTOES):
    """Completa `final` até n itens, sempre com o primeiro item de `pool` (melhor ranqueado)
    que respeita os limites por tema: no máximo MAX_TEMA_TOP6 nas posições 1-6 e
    MAX_TEMA_TOP10 nas posições 1-10. Um item pulado numa posição pode entrar numa posição
    mais adiante. Se os limites esgotarem o pool, completa na ordem original."""
    final = list(final)
    for _ in range(n - len(final)):
        pos = len(final)
        usados = set(final)
        top10 = Counter(tema_de(s) for s in final)
        top6 = Counter(tema_de(s) for s in final[:6])
        escolhido = None
        for s in pool:
            if s in usados:
                continue
            t = tema_de(s)
            if top10[t] >= MAX_TEMA_TOP10 or (pos < 6 and top6[t] >= MAX_TEMA_TOP6):
                continue
            escolhido = s
            break
        if escolhido is None:  # limites esgotaram o pool: relaxa
            escolhido = next((s for s in pool if s not in usados), None)
        if escolhido is None:
            break
        final.append(escolhido)
    return final


def _ordem_final(pool, tema_de, dominante=None, n=MAX_SUGESTOES):
    """`pool`: ids do melhor para o pior ranqueado (sem os já usados). Devolve (ids, promovido).

    1) diversidade por tema (ver _completar);
    2) se há tema dominante no histórico, o melhor item desse tema entra nas 3 primeiras
       posições; `promovido` é esse item quando ele precisou ser puxado (senão None).
    """
    final = _completar([], pool, tema_de, n)
    promovido = None
    if dominante:
        melhor = next((s for s in pool if tema_de(s) == dominante), None)
        if melhor is not None and melhor not in final[:3]:
            final = _completar(final[:2] + [melhor], pool, tema_de, n)
            promovido = melhor
    return final, promovido


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

    # Pool para a ordenação final: o ranking acima e, depois dele, os mais procurados
    # (afinidade com a faixa etária primeiro), para preencher quando a diversidade pedir.
    motivos = {x["servico_id"]: x["motivo"] for x in itens}
    afin = D["afinidade"].get(c["faixa_etaria"], {})
    extras = [s for s in D["ordem"] if s not in usados and s not in motivos]
    extras = ([s for s in extras if afin.get(s, 1.0) >= AFINIDADE_MIN]
              + [s for s in extras if afin.get(s, 1.0) < AFINIDADE_MIN])
    for s in extras:
        motivos[s] = "É um dos serviços mais procurados no Conecta Recife."

    def tema_de(sid):
        t = servicos[sid]["temas"]
        return t[0] if t else sid  # serviço sem tema não conta contra o limite de ninguém

    dominante = D["dominante"].get(cid)
    ids, promovido = _ordem_final([x["servico_id"] for x in itens] + extras, tema_de, dominante)
    if promovido:
        motivos[promovido] = f"Combina com o que você mais usa: {_nome_tema(dominante)}."

    saida = []
    for posicao, sid in enumerate(ids, 1):
        s = servicos[sid]
        tema_int = next((t for t, n in zip(s["temas"], s["temas_norm"]) if n in interesses), None)
        saida.append({
            "posicao": posicao,
            "servico_id": sid,
            "titulo": s["titulo"],
            "tema": tema_int or (s["temas"][0] if s["temas"] else ""),
            "motivo": motivos[sid],
        })
    return saida


def _frase_proxima(motivo, anonimo=False):
    """Frase de até 12 palavras a partir do motivo da primeira sugestão."""
    if anonimo:  # frases próprias da home anônima; as dos logados não mudam
        if motivo == "Muito procurado no Recife.":
            return "Muita gente no Recife usa este serviço."
        a = re.match(r"^Você escolheu o assunto (.+)\.$", motivo or "")
        if a:
            return f"Você escolheu {a.group(1)}, então separamos este serviço."
    m = re.match(r"^Costuma vir depois de (.+)$", motivo or "")
    if m:
        ini, fim = "Depois de", ", muita gente precisa disto."
    else:
        m = re.match(r"^Quem usou (.+) também costuma usar este$", motivo or "")
        if not m:
            return "Muita gente da sua idade usa este serviço."
        ini, fim = "Quem usa", " costuma precisar disto."
    palavras = m.group(1).split()
    fixas = len(ini.split()) + len(fim.split())
    maximo = 12 - fixas
    if len(palavras) > maximo:  # nome longo: corta e marca com reticências
        palavras = palavras[:maximo]
        palavras[-1] = palavras[-1].rstrip(",;:.") + "…"
    return f"{ini} {' '.join(palavras)}{fim}"


def _completar_em_alta(em_alta, ja):
    """Reserva: completa o "em alta" com os serviços em alta no Recife (geral)."""
    if len(em_alta) >= MAX_EM_ALTA:
        return
    servicos = D["servicos"]
    n = D["n_semanas"]
    periodo = "na última semana" if n == 1 else f"nas últimas {n} semanas"
    for sid, cliques in D["em_alta"]:
        if sid in ja or any(e["servico_id"] == sid for e in em_alta):
            continue
        em_alta.append({"servico_id": sid, "titulo": servicos[sid]["titulo"],
                        "motivo": f"Em alta no Recife: {_milhar(cliques)} acessos {periodo}."})
        if len(em_alta) == MAX_EM_ALTA:
            break


def _proxima_acao(sugestoes, anonimo=False):
    if not sugestoes:
        return None
    p = sugestoes[0]
    return {"servico_id": p["servico_id"], "titulo": p["titulo"], "frase": _frase_proxima(p["motivo"], anonimo)}


MAX_INTERESSE_ANONIMO = 2   # serviços de interesse na home anônima (posições 1 e 2)


def _sugestoes_anonimas(interesses):
    """Serviços dos temas escolhidos (revezando entre os temas) nas posições 1 e 2;
    o resto é o top geral por uso. Sem interesses válidos, é só o top geral."""
    servicos = D["servicos"]
    ordem = D["ordem"]
    nomes = {_norm(t): t for t in D["temas"]}
    filas = []  # (tema, nome amigável, serviços mais usados do tema)
    for t, id_ in interesses:
        if t in nomes:
            amigavel = NOMES_AMIGAVEIS.get(id_) if id_ else None
            filas.append((nomes[t], amigavel or nomes[t], [s for s in ordem if t in servicos[s]["temas_norm"]]))
    escolhidos = []  # (servico_id, tema, nome amigável)
    ids = set()
    while len(escolhidos) < MAX_INTERESSE_ANONIMO and any(f for _, _, f in filas):
        for tema, amigavel, fila in filas:
            while fila and fila[0] in ids:
                fila.pop(0)
            if fila and len(escolhidos) < MAX_INTERESSE_ANONIMO:
                sid = fila.pop(0)
                escolhidos.append((sid, tema, amigavel))
                ids.add(sid)
    itens = [(sid, tema, f"Você escolheu o assunto {amigavel}.") for sid, tema, amigavel in escolhidos]
    for sid in ordem:
        if len(itens) == MAX_SUGESTOES:
            break
        if sid not in ids:
            s = servicos[sid]
            itens.append((sid, s["temas"][0] if s["temas"] else "", "Muito procurado no Recife."))
    return [{"posicao": pos, "servico_id": sid, "titulo": servicos[sid]["titulo"], "tema": tema, "motivo": motivo}
            for pos, (sid, tema, motivo) in enumerate(itens[:MAX_SUGESTOES], 1)]


def montar_home_anonima(interesses_txt):
    sugestoes = _sugestoes_anonimas(_interesses_itens(interesses_txt))
    em_alta = []
    _completar_em_alta(em_alta, {x["servico_id"] for x in sugestoes})
    return {
        "cidadao": {"id": None, "faixa_etaria": None, "resumo": "Visitante", "modo_simples": False},
        "continue": [],
        "sugestoes": sugestoes,
        "em_alta": em_alta,
        "proxima_acao": _proxima_acao(sugestoes, anonimo=True),
    }


def montar_home(cid, interesses_txt, consentimento):
    c = D["cidadaos"][cid]
    servicos = D["servicos"]
    usos = D["usos"].get(cid, {})
    interesses = _interesses(interesses_txt)

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
    _completar_em_alta(em_alta, ja)


    return {
        "cidadao": {
            "id": cid,
            "faixa_etaria": c["faixa_etaria"],
            "rpa": c["rpa"],
            "resumo": _resumo(c, consentimento),
            "modo_simples": c["idade"] >= 60 or c["pessoa_com_deficiencia"],
            "tem_historico": cid in D["com_historico"],
        },
        "continue": continuar,
        "sugestoes": sugestoes,
        "em_alta": em_alta,
        "proxima_acao": _proxima_acao(sugestoes),
    }


# ----------------------------------------------------------------------------
# Handler
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Solicitações (sem armazenamento: o protocolo carrega o horário de abertura)
# ----------------------------------------------------------------------------
FUSO_RECIFE = timezone(timedelta(hours=-3))
PREFIXO_PROTOCOLO = "CR-2026-"
PRAZO_ESTIMADO = "até 5 dias úteis"
ETAPAS = ("Recebido", "Em análise", "Equipe a caminho", "Concluído")
LIMITES_ETAPA_SEG = (60, 180, 300)   # fim de Recebido, Em análise, Equipe a caminho
PROXIMOS_PASSOS = [
    "Sua solicitação foi registrada e já está na fila.",
    "Nossa equipe vai analisar o pedido e o endereço informado.",
    "Guarde o protocolo para acompanhar o andamento.",
]
OBRIGATORIOS = ("cidadao_id", "servico_id", "tipo", "endereco", "descricao")
INICIO_PROTOCOLOS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _base36(n):
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _BASE36[r] + out
    return out or "0"


def _gerar_protocolo(segundos):
    return PREFIXO_PROTOCOLO + _base36(segundos).rjust(6, "0")


def _ler_protocolo(texto):
    """Devolve os segundos desde 1970 contidos no protocolo, ou None se não for válido."""
    t = (texto or "").strip().upper()
    if not re.fullmatch(re.escape(PREFIXO_PROTOCOLO) + r"[0-9A-Z]{6}", t):
        return None
    segundos = int(t[len(PREFIXO_PROTOCOLO):], 36)
    if segundos < INICIO_PROTOCOLOS or segundos > time.time() + 60:  # nada de datas absurdas ou futuras
        return None
    return segundos


def _etapa_atual(segundos_desde_abertura):
    for i, limite in enumerate(LIMITES_ETAPA_SEG):
        if segundos_desde_abertura < limite:
            return i
    return len(ETAPAS) - 1


def _solicitacao_json(segundos_abertura, titulo, agora):
    atual = _etapa_atual(max(agora - segundos_abertura, 0))
    quando = datetime.fromtimestamp(segundos_abertura, FUSO_RECIFE)
    return {
        "protocolo": _gerar_protocolo(segundos_abertura),
        "servico": titulo,
        "data_abertura": quando.strftime("%d/%m/%Y %H:%M"),
        "prazo_estimado": PRAZO_ESTIMADO,
        "proximos_passos": list(PROXIMOS_PASSOS),
        "linha_do_tempo": [
            {"etapa": nome, "status": "feito" if i < atual else "atual" if i == atual else "pendente"}
            for i, nome in enumerate(ETAPAS)],
    }


def _texto(v):
    return str(v).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def criar_solicitacao(event):
    corpo = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            corpo = base64.b64decode(corpo).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return _resposta(400, {"erro": "O corpo da requisição não pôde ser lido. Envie um JSON em UTF-8."})
    try:
        dados = json.loads(corpo)
    except ValueError:
        return _resposta(400, {"erro": "O corpo da requisição deve ser um JSON válido."})
    if not isinstance(dados, dict):
        return _resposta(400, {"erro": "O corpo da requisição deve ser um objeto JSON com cidadao_id, servico_id e campos."})
    campos = dados.get("campos")
    if campos is None:
        campos = {}
    if not isinstance(campos, dict):
        return _resposta(400, {"erro": "O campo 'campos' deve ser um objeto com tipo, endereco, referencia e descricao."})
    valores = {k: _texto(dados.get(k)) for k in ("cidadao_id", "servico_id")}
    valores.update({k: _texto(campos.get(k)) for k in ("tipo", "endereco", "descricao")})
    faltam = [k for k in OBRIGATORIOS if not valores[k]]
    if faltam:
        return _resposta(400, {"erro": "Faltam campos obrigatórios: " + ", ".join(faltam) + "."})
    if valores["cidadao_id"].upper() not in D["cidadaos"]:
        return _resposta(404, {"erro": "cidadão não encontrado"})
    servico = D["servicos"].get(valores["servico_id"])
    if not servico:
        return _resposta(404, {"erro": "serviço não encontrado"})
    agora = int(time.time())
    return _resposta(201, _solicitacao_json(agora, servico["titulo"], agora))


def consultar_solicitacao(protocolo, servico_id):
    segundos = _ler_protocolo(protocolo)
    if segundos is None:
        return _resposta(404, {"erro": "protocolo não encontrado"})
    if not servico_id:
        return _resposta(400, {"erro": "Informe o parâmetro servico_id. Exemplo: /solicitacoes/CR-2026-ABC123?servico_id=199"})
    servico = D["servicos"].get(servico_id)
    if not servico:
        return _resposta(404, {"erro": "serviço não encontrado"})
    return _resposta(200, _solicitacao_json(segundos, servico["titulo"], int(time.time())))


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

        if caminho == "/solicitacoes":
            if metodo != "POST":
                return _resposta(405, {"erro": "Método não permitido. Use POST."})
            return criar_solicitacao(event)
        if caminho.startswith("/solicitacoes/"):
            if metodo not in ("GET", "HEAD"):
                return _resposta(405, {"erro": "Método não permitido. Use GET."})
            return consultar_solicitacao(unquote_plus(caminho[len("/solicitacoes/"):]), _param(qs, "servico_id"))

        if metodo not in ("GET", "HEAD"):
            return _resposta(405, {"erro": "Método não permitido. Use GET."})

        if caminho == "/temas":
            return _resposta(200, {"temas": D["temas"]})

        if caminho == "/home":
            cid = _param(qs, "cidadao_id").upper()
            if not cid:  # sem cidadao_id: home anônima (visitante)
                return _resposta(200, montar_home_anonima(_param(qs, "interesses")))
            if cid not in D["cidadaos"]:
                return _resposta(404, {"erro": "cidadão não encontrado"})
            return _resposta(200, montar_home(cid, _param(qs, "interesses"),
                                              _bool(_param(qs, "consentimento_saude"))))

        return _resposta(404, {"erro": f"Rota '{caminho}' não existe. Use /temas, /home ou /solicitacoes."})
    except Exception as e:  # nunca devolver erro que não seja JSON
        print(f"ERRO: {type(e).__name__}: {e}")
        return _resposta(500, {"erro": "Erro interno ao montar a resposta. Tente novamente."})


# Carga na inicialização (cold start) quando estiver na AWS.
if os.environ.get("BUCKET"):
    carregar_do_s3()
