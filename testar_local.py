# -*- coding: utf-8 -*-
"""Teste local das rotas, lendo os CSVs do disco (sem S3 e sem boto3).

Uso:  py testar_local.py
Procura os CSVs em .\\dados\\ ; se não existir, usa a pasta deste script.

Chama /temas, /servicos, /home (três cidadãos e a home anônima), /solicitacoes e os casos de erro.

Também precisa da pasta .\\saida\\ (ao lado deste script) com os arquivos do modelo:
  reco_api.json.gz, afinidade_faixa.json e em_alta_faixa.json
baixados de s3://claudinhas-dados-hack/saida/
"""
import json
import os
import sys
import time

os.environ.pop("BUCKET", None)  # garante que nada vai ao S3
import lambda_function as lf

sys.stdout.reconfigure(encoding="utf-8")

AQUI = os.path.dirname(os.path.abspath(__file__))
PASTA = os.path.join(AQUI, "dados")
if not os.path.exists(os.path.join(PASTA, "servicos.csv")):
    PASTA = AQUI
PASTA_SAIDA = os.path.join(AQUI, "saida")
ARQUIVOS_SAIDA = ("reco_api.json.gz", "afinidade_faixa.json", "em_alta_faixa.json")

CASOS = [
    ("C007938", {"interesses": "Saúde|Educação", "consentimento_saude": "true"}),
    ("C068579", {"interesses": "PJ e Autônomo", "consentimento_saude": "false"}),
    ("C076260", {}),
]


def chamar(caminho, qs=None, metodo="GET", corpo=None):
    evento = {"rawPath": caminho, "queryStringParameters": qs or None,
              "requestContext": {"http": {"method": metodo}}}
    if corpo is not None:
        evento["body"] = json.dumps(corpo, ensure_ascii=False)
    t0 = time.perf_counter()
    r = lf.lambda_handler(evento, None)
    ms = (time.perf_counter() - t0) * 1000
    assert r["headers"]["Content-Type"].startswith("application/json"), r["headers"]
    return r["statusCode"], json.loads(r["body"]), ms


def main():
    faltam = [n for n in ARQUIVOS_SAIDA if not os.path.exists(os.path.join(PASTA_SAIDA, n))]
    if faltam:
        sys.exit(f"Faltam em {PASTA_SAIDA}: {', '.join(faltam)}\n"
                 "Baixe de s3://claudinhas-dados-hack/saida/ antes de rodar o teste.")

    t0 = time.perf_counter()
    lf.carregar_da_pasta(PASTA, PASTA_SAIDA)
    print(f"CSVs lidos de {PASTA} e modelo de {PASTA_SAIDA} em {time.perf_counter() - t0:.1f}s "
          f"({len(lf.D['servicos'])} serviços, {len(lf.D['cidadaos'])} cidadãos)\n")

    status, corpo, ms = chamar("/temas")
    print(f"GET /temas -> {status} em {ms:.1f} ms | {len(corpo['temas'])} temas")
    print("  " + " · ".join(corpo["temas"]) + "\n")

    # --- busca de serviços ---
    for q in ("iptu", "saude consulta", "x", "zzzzqqq"):
        status, b, ms = chamar("/servicos", {"q": q})
        print(f"GET /servicos?q={q} -> {status} em {ms:.1f} ms | total {b['total']}"
              + (f" | {b['mensagem']}" if "mensagem" in b else ""))
        for x in b["resultados"][:3]:
            print(f"    [{x['servico_id']:>4}] {x['titulo']}  ({x['tema']}, {x['tipo_acao']})")
        assert status == 200 and len(b["resultados"]) <= 10
    print()

    for cid, extra in CASOS:
        qs = {"cidadao_id": cid, **extra}
        status, h, ms = chamar("/home", qs)
        consulta = "&".join(f"{k}={v}" for k, v in qs.items())
        print("=" * 100)
        print(f"GET /home?{consulta} -> {status} em {ms:.1f} ms")
        c = h["cidadao"]
        print(f"  cidadão: {c['id']} | faixa {c['faixa_etaria']} | RPA {c['rpa']} | modo_simples={c['modo_simples']}")
        print(f"  resumo : {c['resumo']}")
        print("  CONTINUE")
        for x in h["continue"]:
            print(f"    [{x['servico_id']:>4}] {x['titulo']}\n           -> {x['motivo']}")
        print("  SUGESTÕES")
        for x in h["sugestoes"]:
            print(f"    {x['posicao']:>2}. [{x['servico_id']:>4}] {x['titulo']}  ({x['tema']})\n           -> {x['motivo']}")
        print("  EM ALTA")
        for x in h["em_alta"]:
            print(f"    [{x['servico_id']:>4}] {x['titulo']}\n           -> {x['motivo']}")
        p = h.get("proxima_acao")
        if p:
            print(f"  PRÓXIMA AÇÃO\n    [{p['servico_id']:>4}] {p['titulo']}\n           -> {p['frase']}")
        usados = set(lf.D["usos"].get(cid, {}))
        assert not usados & {x["servico_id"] for x in h["sugestoes"]}, "sugestão repetiu serviço já usado"
        assert ms < 1000, "requisição passou de 1 segundo"
        print()

    # --- home anônima (sem cidadao_id) ---
    print("=" * 100)
    for qs in (None, {"interesses": "saude,emprego"}):
        status, h, ms = chamar("/home", qs)
        print(f"GET /home {qs or '(sem parâmetros)'} -> {status} em {ms:.1f} ms | resumo: {h['cidadao']['resumo']}")
        for x in h["sugestoes"]:
            print(f"    {x['posicao']:>2}. [{x['servico_id']:>4}] {x['titulo']}  ({x['tema']})\n           -> {x['motivo']}")
        assert status == 200 and h["cidadao"]["id"] is None and h["continue"] == []
        print()

    # --- solicitações ---
    print("=" * 100)
    sid = lf.D["ordem"][0]
    pedido = {"cidadao_id": CASOS[0][0], "servico_id": sid,
              "campos": {"tipo": "Teste", "endereco": "Rua da Aurora, 100",
                         "referencia": "Em frente à praça", "descricao": "Solicitação de teste local."}}
    status, s, ms = chamar("/solicitacoes", metodo="POST", corpo=pedido)
    print(f"POST /solicitacoes -> {status} em {ms:.1f} ms")
    print("  " + json.dumps(s, ensure_ascii=False))
    assert status == 201 and s["protocolo"].startswith("CR-2026-")
    status, s2, ms = chamar(f"/solicitacoes/{s['protocolo']}", {"servico_id": sid})
    print(f"GET /solicitacoes/{s['protocolo']}?servico_id={sid} -> {status} em {ms:.1f} ms")
    print("  linha do tempo: " + " > ".join(f"{e['etapa']} ({e['status']})" for e in s2["linha_do_tempo"]))
    assert status == 200 and s2["protocolo"] == s["protocolo"]
    sem_servico = {"cidadao_id": pedido["cidadao_id"], "campos": pedido["campos"]}
    status, s3, ms = chamar("/solicitacoes", metodo="POST", corpo=sem_servico)
    print(f"POST /solicitacoes (sem servico_id) -> {status} | serviço: {s3.get('servico')}")
    assert status == 201
    print()

    # --- erros ---
    print("=" * 100)
    erros = (
        ("GET", "/home", {"cidadao_id": "C999999"}, None, 404),
        ("GET", "/outra", None, None, 404),
        ("GET", "/solicitacoes/CR-2026-ZZZZZZ", {"servico_id": sid}, None, 404),
        ("POST", "/solicitacoes", None, {"cidadao_id": CASOS[0][0]}, 400),
        ("GET", "/solicitacoes", None, None, 405),
    )
    for metodo, caminho, qs, corpo_req, esperado in erros:
        status, corpo, ms = chamar(caminho, qs, metodo=metodo, corpo=corpo_req)
        print(f"{metodo} {caminho} {qs or ''} -> {status} {json.dumps(corpo, ensure_ascii=False)}")
        assert status == esperado, f"esperava {esperado}"
    print("\nOK: sugestões não repetem serviços usados e todas as respostas vieram em menos de 1 s.")


if __name__ == "__main__":
    main()
