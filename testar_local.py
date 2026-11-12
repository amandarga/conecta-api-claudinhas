# -*- coding: utf-8 -*-
"""Teste local das rotas, lendo os CSVs do disco (sem S3 e sem boto3).

Uso:  python testar_local.py
Procura os CSVs em .\\dados\\ ; se não existir, usa a pasta deste script.
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

CASOS = [
    ("C007938", {"interesses": "Saúde|Educação", "consentimento_saude": "true"}),
    ("C068579", {"interesses": "PJ e Autônomo", "consentimento_saude": "false"}),
    ("C076260", {}),
]


def chamar(caminho, qs=None):
    t0 = time.perf_counter()
    r = lf.lambda_handler({"rawPath": caminho, "queryStringParameters": qs or None}, None)
    ms = (time.perf_counter() - t0) * 1000
    assert r["headers"]["Content-Type"].startswith("application/json"), r["headers"]
    return r["statusCode"], json.loads(r["body"]), ms


def main():
    t0 = time.perf_counter()
    lf.carregar_da_pasta(PASTA)
    print(f"CSVs lidos de {PASTA} em {time.perf_counter() - t0:.1f}s "
          f"({len(lf.D['servicos'])} serviços, {len(lf.D['cidadaos'])} cidadãos)\n")

    status, corpo, ms = chamar("/temas")
    print(f"GET /temas -> {status} em {ms:.1f} ms | {len(corpo['temas'])} temas")
    print("  " + " · ".join(corpo["temas"]) + "\n")

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
        usados = set(lf.D["usos"].get(cid, {}))
        assert not usados & {x["servico_id"] for x in h["sugestoes"]}, "sugestão repetiu serviço já usado"
        assert ms < 1000, "requisição passou de 1 segundo"
        print()

    print("=" * 100)
    for caminho, qs in (("/home", {"cidadao_id": "C999999"}), ("/home", None), ("/outra", None)):
        status, corpo, ms = chamar(caminho, qs)
        print(f"GET {caminho} {qs or ''} -> {status} {json.dumps(corpo, ensure_ascii=False)}")
    print("\nOK: sugestões não repetem serviços usados e todas as respostas vieram em menos de 1 s.")


if __name__ == "__main__":
    main()
