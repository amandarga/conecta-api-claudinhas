#!/usr/bin/env python3
"""Hiperpersonalizacao Conecta Recife: validacao offline e geracao de recomendacoes.

Uso (pelo venv):
  ~/reco/.venv/bin/python ~/reco/recomendar.py validar
Dados ficam em ~/reco/dados (nao copiar para fora). Se existir
dados/visitas_sem_login.csv ele entra automaticamente nas sessoes (modelos c e d).
"""
import os
import sys
import numpy as np
import pandas as pd
from scipy import sparse

BASE = os.path.expanduser('~/reco')
D = f'{BASE}/dados'
K = 10
RNG = np.random.default_rng(42)
CHUNK = 6000
MAX_SHARE = 0.30   # origem do motivo: ignora servicos usados por mais de 30% dos cidadaos
MAX_ORIGEM = 3     # um servico de origem aparece em no maximo 3 motivos por cidadao
TIE = 1e-4  # peso minimo de (b) para desempate nos modelos c e d


# ----------------------------------------------------------------- dados
def load():
    r = lambda f: pd.read_csv(f'{D}/{f}', keep_default_na=False)
    serv, cid, hist, uso = r('servicos.csv'), r('cidadaos.csv'), r('historico.csv'), r('uso_semanal.csv')
    vis = r('visitas_sem_login.csv') if os.path.exists(f'{D}/visitas_sem_login.csv') else None
    return serv, cid, hist, uso, vis


def prepare(serv, cid, hist, uso, vis):
    sidx = {v: i for i, v in enumerate(serv.servico_id)}
    uidx = {v: i for i, v in enumerate(cid.cidadao_id)}
    h = hist.copy()
    h['s'] = h.servico_id.map(sidx)
    h['u'] = h.cidadao_id.map(uidx)
    bad = h.s.isna() | h.u.isna()
    if bad.any():
        print(f'aviso: {bad.sum()} linhas do historico descartadas (servico/cidadao desconhecido)')
    h = h[~bad].copy()
    h['s'] = h.s.astype(int)
    h['u'] = h.u.astype(int)
    h['data'] = pd.to_datetime(h.data)
    h['ordem'] = pd.to_numeric(h.ordem_na_sessao)
    sess = h[['sessao_id', 'ordem', 's', 'data']].copy()
    if vis is not None:
        need = {'sessao_id', 'servico_id', 'data'}
        if need <= set(vis.columns):
            v = vis.copy()
            v['s'] = v.servico_id.map(sidx)
            v = v[v.s.notna()].copy()
            v['s'] = v.s.astype(int)
            v['data'] = pd.to_datetime(v.data)
            v['ordem'] = pd.to_numeric(v['ordem_na_sessao']) if 'ordem_na_sessao' in v else v.groupby('sessao_id').cumcount()
            v['sessao_id'] = 'v_' + v.sessao_id.astype(str)
            sess = pd.concat([sess, v[['sessao_id', 'ordem', 's', 'data']]], ignore_index=True)
            print(f'visitas_sem_login incluido: {len(v)} linhas')
        else:
            print('aviso: visitas_sem_login.csv sem colunas esperadas', need, '- ignorado')
    # grupo de cidadaos parecidos (modelo b)
    t = lambda c: cid[c].astype(str)
    mei_emp = ((cid.mei == 'True') | (cid.empreendedor == 'True')).astype(str)
    key = t('faixa_etaria') + '|' + t('cadastro_unico') + '|' + mei_emp + '|' + t('pessoa_com_deficiencia') \
        + '|' + t('criancas_na_familia') + '|' + t('gestante')
    grp = pd.factorize(key)[0]
    u = uso.copy()
    u['semana'] = pd.to_datetime(u.semana)
    u['visitantes'] = pd.to_numeric(u.visitantes, errors='coerce').fillna(0)
    u['s'] = u.servico_id.map(sidx)
    u = u[u.s.notna()].copy()
    u['s'] = u.s.astype(int)
    return dict(hist=h.sort_values(['u', 'data', 'hora', 'ordem']), sess=sess, uso=u, grp=grp,
                n_users=len(cid), n_serv=len(serv), sidx=sidx)


# ----------------------------------------------------------------- treino
def binmat(rows, cols, shape):
    m = sparse.csr_matrix((np.ones(len(rows), np.float32), (rows, cols)), shape=shape)
    m.data[:] = 1
    return m


def fit(P, cutoff):
    """Treina tudo usando apenas dados com data <= cutoff."""
    S, N = P['n_serv'], P['n_users']
    h = P['hist'][P['hist'].data <= cutoff]
    U = binmat(h.u.values, h.s.values, (N, S))
    active = np.asarray(U.sum(1)).ravel() > 0
    # (a) popularidade entre cidadaos com historico
    pop_rate = np.asarray(U[active].sum(0)).ravel() / max(active.sum(), 1)
    # (b) popularidade entre parecidos, suavizada pela geral
    g = P['grp']
    G = g.max() + 1
    Gm = sparse.csr_matrix((active.astype(np.float32), (g, np.arange(N))), shape=(G, N))
    cnt = np.asarray((Gm @ U).todense())
    ng = np.asarray(Gm.sum(1)).ravel()[:, None]
    k = 20.0
    rate = ((cnt + k * pop_rate[None, :]) / (ng + k)).astype(np.float32)
    # sessoes
    s = P['sess'][P['sess'].data <= cutoff].sort_values(['sessao_id', 'ordem'], kind='stable')
    sid, sv = s.sessao_id.values, s.s.values
    # (c) transicoes entre servicos consecutivos na sessao
    same = sid[1:] == sid[:-1]
    a, b = sv[:-1][same], sv[1:][same]
    keep = a != b
    T = np.bincount(a[keep] * S + b[keep], minlength=S * S).reshape(S, S).astype(np.float32)
    Tn = T / (T.sum(1, keepdims=True) + 5.0)
    # ultimos 3 servicos distintos do cidadao (mais recente pesa mais)
    hd = h.drop_duplicates(['u', 's'], keep='last').copy()
    hd['r'] = hd.groupby('u').cumcount(ascending=False)
    hd = hd[hd.r < 3]
    L = sparse.csr_matrix((np.array([1.0, 0.6, 0.36], np.float32)[hd.r.values], (hd.u.values, hd.s.values)), shape=(N, S))
    # (d) coocorrencia (cosseno com encolhimento) por cidadao e por sessao
    def cooc(M):
        C = (M.T @ M).toarray().astype(np.float32)
        n = np.diag(C).copy()
        Cn = C / (np.sqrt(np.outer(n, n)) + 10.0)
        np.fill_diagonal(Cn, 0)
        return Cn, C
    Cu, Cu_raw = cooc(U)
    codes = pd.factorize(sid)[0]
    Sm = binmat(codes, sv, (codes.max() + 1, S))
    Cs, Cs_raw = cooc(Sm)
    share = np.diag(Cu_raw) / N  # fracao dos cidadaos que usaram cada servico (so para o texto do motivo)
    # tendencia recente (uso_semanal): ultimas 4 semanas vs 8 anteriores
    u = P['uso'][P['uso'].semana <= cutoff]
    weeks = np.sort(u.semana.unique())
    rec_w, prev_w = weeks[-4:], weeks[-12:-4]
    rec = np.zeros(S); prv = np.zeros(S)
    for w, arr in ((rec_w, rec), (prev_w, prv)):
        x = u[u.semana.isin(w)].groupby('s').visitantes.sum()
        arr[x.index.values] = x.values
    tr = np.clip(np.log((rec + 1) / (prv / 2 + 1)), 0, None)
    tr = (tr / (tr.max() + 1e-12)).astype(np.float32)
    return dict(U=U, active=active, pop=pop_rate.astype(np.float32), rate=rate, g=g, Tn=Tn, L=L,
                Cu=Cu, Cs=Cs, tr=tr, S=S, Cu_raw=Cu_raw, Cs_raw=Cs_raw, share=share)


def norm(x):
    return x / (x.max(1, keepdims=True) + 1e-12)


def comps(st, rows):
    """Escores normalizados (max=1 por cidadao) de cada componente."""
    Uc = st['U'][rows]
    n = len(rows)
    return dict(
        a=np.tile(st['pop'], (n, 1)),
        b=norm(st['rate'][st['g'][rows]]),
        c=norm(np.asarray(st['L'][rows] @ st['Tn'])),
        d1=norm(np.asarray(Uc @ st['Cu'])),
        d2=norm(np.asarray(Uc @ st['Cs'])),
        t=np.tile(st['tr'], (n, 1)),
    ), Uc.toarray() > 0


def combine(cp, used, w):
    sc = sum(v * cp[k] for k, v in w.items() if v)
    return np.where(used, -1e9, sc)


def topk(sc, k=K):
    idx = np.argpartition(-sc, k, axis=1)[:, :k]
    v = np.take_along_axis(sc, idx, 1)
    return np.take_along_axis(idx, np.argsort(-v, axis=1), 1)


def metrics(top, Y):
    hit = np.take_along_axis(Y, top, 1) > 0
    any_ = hit.any(1)
    first = hit.argmax(1)
    return any_.sum(), np.where(any_, 1.0 / (first + 1), 0.0).sum()


MODELOS = {
    'a) popularidade geral': {'a': 1},
    'b) populares entre parecidos': {'b': 1},
    'c) proximo passo': {'c': 1, 'b': TIE},
    'd1) coocorrencia por cidadao': {'d1': 1, 'b': TIE},
    'd2) coocorrencia por sessao': {'d2': 1, 'b': TIE},
    'd) coocorrencia (cidadao+sessao)': {'d1': .5, 'd2': .5, 'b': TIE},
}


def evaluate(st, rows, Y, models):
    """Y: matriz esparsa (cidadaos x servicos) com os servicos novos do periodo-alvo."""
    tot = {m: [0, 0.0] for m in models}
    for i in range(0, len(rows), CHUNK):
        r = rows[i:i + CHUNK]
        cp, used = comps(st, r)
        Yc = Y[r].toarray()
        for m, w in models.items():
            h, mr = metrics(topk(combine(cp, used, w)), Yc)
            tot[m][0] += h; tot[m][1] += mr
    n = len(rows)
    return {m: (v[0] / n, v[1] / n) for m, v in tot.items()}


def targets(P, st, lo, hi):
    """Servicos usados em (lo, hi] que o cidadao nunca tinha usado ate lo (treino st)."""
    t = P['hist']
    t = t[(t.data > lo) & (t.data <= hi)]
    Tm = binmat(t.u.values, t.s.values, (P['n_users'], P['n_serv']))
    Y = Tm.maximum(st['U']) - st['U']
    Y.eliminate_zeros()
    rows = np.where(np.asarray(Y.sum(1)).ravel() > 0)[0]
    return Y, rows


# ----------------------------------------------------------------- pesos de (e)
def tune_weights(P, cutoff, n_sample=15000, n_rand=120, n_local=80):
    """Ajusta pesos num periodo de validacao interno (4 semanas antes do teste)."""
    c2 = cutoff - pd.Timedelta(days=28)
    st = fit(P, c2)
    Y, rows = targets(P, st, c2, cutoff)
    rows = RNG.choice(rows, min(n_sample, len(rows)), replace=False)
    cp, used = comps(st, rows)
    Yc = Y[rows].toarray()
    names = ['b', 'c', 'd1', 'd2', 't']

    def score(w):
        h, m = metrics(topk(combine(cp, used, w)), Yc)
        return (h + m) / len(rows)

    best, bw = -1, None
    for _ in range(n_rand):
        w = dict(zip(names, RNG.dirichlet(np.ones(len(names)) * 0.7)))
        s = score(w)
        if s > best: best, bw = s, w
    for _ in range(n_local):
        w = {k: max(v * np.exp(RNG.normal(0, 0.4)), 0) for k, v in bw.items()}
        z = sum(w.values()); w = {k: v / z for k, v in w.items()}
        s = score(w)
        if s > best: best, bw = s, w
    print(f'pesos ajustados (validacao interna, hit+MRR={best:.4f}):', {k: round(v, 3) for k, v in bw.items()})
    return bw


# ----------------------------------------------------------------- validar
def validar():
    P = prepare(*load())
    mx = P['hist'].data.max()
    cutoff = mx - pd.Timedelta(days=28)
    print(f'historico: {P["hist"].data.min().date()} a {mx.date()} | treino ate {cutoff.date()} | teste {cutoff.date()} < data <= {mx.date()}')
    w = tune_weights(P, cutoff)
    st = fit(P, cutoff)
    Y, rows = targets(P, st, cutoff, mx)
    frio = (~st['active'][rows]).mean()
    print(f'cidadaos avaliados (>=1 servico novo no teste): {len(rows)} | sem historico no treino: {frio:.1%}')
    models = dict(MODELOS)
    models['e) combinacao ponderada (b,c,d,tendencia)'] = w
    res = evaluate(st, rows, Y, models)
    tab = pd.DataFrame([(m, h, r) for m, (h, r) in res.items()], columns=['modelo', 'hit_rate@10', 'MRR@10'])
    print(tab.to_string(index=False, float_format=lambda x: f'{x:.4f}'))
    tab.to_csv(f'{BASE}/metricas.csv', index=False)
    pd.Series(w).to_json(f'{BASE}/pesos_e.json')


# ----------------------------------------------------------------- gerar
def nomes_curtos(serv):
    n = serv.nome_resumido.where(serv.nome_resumido.str.strip() != '', serv.nome)
    n = n.where(n.str.strip() != '', serv.item_nome)
    return n.str.strip().tolist()


def gerar():
    import json
    serv, cid, hist, uso, vis = load()
    P = prepare(serv, cid, hist, uso, vis)
    nomes = nomes_curtos(serv)
    sids = serv.servico_id.values
    w = json.load(open(f'{BASE}/pesos_e.json'))
    mx = P['hist'].data.max()
    st = fit(P, mx)
    print(f'treino com todo o historico ate {mx.date()} | pesos: { {k: round(v, 3) for k, v in w.items()} }')
    N = P['n_users']
    out_s = np.zeros((N, K), int)
    out_m = np.empty((N, K), object)
    FRASE = {'b': 'Muito usado por pessoas da sua faixa de idade com perfil parecido',
             't': 'Muito procurado nas últimas semanas'}
    for i in range(0, N, 3000):
        rows = np.arange(i, min(i + 3000, N))
        cp, used = comps(st, rows)
        top = topk(combine(cp, used, w))
        out_s[rows] = top
        keys = list(w)
        contrib = np.stack([w[k] * np.take_along_axis(cp[k], top, 1) for k in keys])  # (k, n, K)
        best = np.array(keys)[contrib.argmax(0)]
        Uc = st['U'][rows].toarray()
        Lc = st['L'][rows].toarray()
        order = np.argsort(-contrib, axis=0)  # (k, n, K): componentes do maior ao menor
        for r in range(len(rows)):
            u = rows[r]
            if not st['active'][u]:
                out_m[u, :] = 'Popular entre pessoas com perfil parecido com o seu'
                continue
            used_j = np.nonzero(Uc[r])[0]
            ok_j = used_j[st['share'][used_j] <= MAX_SHARE]
            lastj = np.nonzero(Lc[r])[0]
            cnt = {}  # vezes que cada servico de origem ja apareceu nos motivos deste cidadao
            for p in range(K):
                it = top[r, p]
                m = None
                for ki in order[:, r, p]:
                    k = keys[ki]
                    if k in ('d1', 'd2'):
                        raw = st['Cu_raw'] if k == 'd1' else st['Cs_raw']
                        if len(ok_j):
                            co = raw[ok_j, it]
                            lift = co / np.maximum(np.diag(raw)[ok_j], 1)  # lift = coocorrencia / popularidade da origem
                            for q in np.argsort(-lift, kind='stable'):  # melhor origem primeiro; se saturada, a proxima
                                j = ok_j[q]
                                if co[q] <= 0:
                                    break
                                if cnt.get(j, 0) < MAX_ORIGEM:
                                    m = f'Quem usou {nomes[j]} também costuma usar este'
                                    cnt[j] = cnt.get(j, 0) + 1
                                    break
                    elif k == 'c':
                        if len(lastj):
                            sc = Lc[r, lastj] * st['Tn'][lastj, it]
                            j = lastj[sc.argmax()]
                            if sc.max() > 0 and cnt.get(j, 0) < MAX_ORIGEM:
                                m = f'Costuma vir depois de {nomes[j]}'
                                cnt[j] = cnt.get(j, 0) + 1
                    elif k == 't':
                        if st['tr'][it] > 0:
                            m = FRASE['t']
                    else:
                        m = FRASE['b']
                    if m:
                        break
                out_m[u, p] = m or FRASE['b']
    df = pd.DataFrame({
        'cidadao_id': np.repeat(cid.cidadao_id.values, K),
        'posicao': np.tile(np.arange(1, K + 1), N),
        'servico_id': sids[out_s.ravel()],
        'motivo': out_m.ravel()})
    df.to_csv(f'{BASE}/recomendacoes.csv', index=False, encoding='utf-8')
    print('gravado', f'{BASE}/recomendacoes.csv', len(df), 'linhas')
    import gzip
    motivos = sorted(df.motivo.unique())
    mi = {m: i for i, m in enumerate(motivos)}
    idx = df.motivo.map(mi).values.reshape(N, K)
    sv_ = df.servico_id.values.reshape(N, K)
    reco = {c: [[int(sv_[n, p]), int(idx[n, p])] for p in range(K)] for n, c in enumerate(cid.cidadao_id.values)}
    with gzip.open(f'{BASE}/reco_api.json.gz', 'wt', encoding='utf-8', compresslevel=9) as f:
        json.dump({'motivos': motivos, 'reco': reco}, f, ensure_ascii=False, separators=(',', ':'))
    print('gravado reco_api.json.gz:', os.path.getsize(f'{BASE}/reco_api.json.gz'), 'bytes |', len(motivos), 'motivos')


def checar(personas=()):
    serv, cid = pd.read_csv(f'{D}/servicos.csv', keep_default_na=False), pd.read_csv(f'{D}/cidadaos.csv', keep_default_na=False)
    r = pd.read_csv(f'{BASE}/recomendacoes.csv', keep_default_na=False, encoding='utf-8')
    nm = dict(zip(serv.servico_id, nomes_curtos(serv)))
    ok = lambda c: 'OK' if c else 'FALHOU'
    print('colunas:', list(r.columns), ok(list(r.columns) == ['cidadao_id', 'posicao', 'servico_id', 'motivo']))
    print('cidadaos distintos:', r.cidadao_id.nunique(), ok(r.cidadao_id.nunique() == 89046 and set(r.cidadao_id) == set(cid.cidadao_id)))
    g = r.groupby('cidadao_id').posicao
    print('10 linhas por cidadao, posicoes 1..10 sem repetir:', ok((g.size() == 10).all() and (g.nunique() == 10).all() and r.posicao.between(1, 10).all()))
    print('servico_id existe em servicos.csv:', ok(r.servico_id.isin(serv.servico_id).all()))
    print('sem servico repetido por cidadao:', ok((r.groupby('cidadao_id').servico_id.nunique() == 10).all()))
    h = pd.read_csv(f'{D}/historico.csv', keep_default_na=False)
    usados = set(zip(h.cidadao_id, h.servico_id))
    print('nenhum servico ja usado:', ok(not any(t in usados for t in zip(r.cidadao_id, r.servico_id))))
    print('motivos vazios:', int((r.motivo.str.strip() == '').sum()), '| motivos distintos:', r.motivo.nunique())
    for c in personas:
        print(f'\n{c}:')
        for _, x in r[r.cidadao_id == c].iterrows():
            print(f'  {x.posicao:>2}. [{x.servico_id}] {nm[x.servico_id]}  ->  {x.motivo}')


def aux_api():
    """afinidade_faixa.json e em_alta_faixa.json (pre-calculo para a API)."""
    import json
    serv, cid, hist, uso, vis = load()
    P = prepare(serv, cid, hist, uso, vis)
    h = P['hist']
    sids = serv.servico_id.values
    faixa = cid.faixa_etaria.values
    N = len(cid)
    # afinidade = taxa de uso na faixa / taxa de uso geral (cidadaos distintos / todos os cidadaos)
    U = binmat(h.u.values, h.s.values, (N, P['n_serv']))
    geral = np.asarray(U.sum(0)).ravel() / N
    afin, alta = {}, {}
    mx = h.data.max()
    rec = h[h.data > mx - pd.Timedelta(days=28)]
    ant = h[(h.data > mx - pd.Timedelta(days=56)) & (h.data <= mx - pd.Timedelta(days=28))]
    for f in sorted(set(faixa)):
        m = faixa == f
        if not f or m.sum() == 0:
            continue
        taxa = np.asarray(U[m].sum(0)).ravel() / m.sum()
        afin[f] = {str(sids[j]): round(float(taxa[j] / geral[j]), 3) for j in np.nonzero(geral > 0)[0]}
        cr = rec[m[rec.u.values]].groupby('s').size().reindex(range(P['n_serv']), fill_value=0).values
        ca = ant[m[ant.u.values]].groupby('s').size().reindex(range(P['n_serv']), fill_value=0).values
        k = 10.0
        score = np.log((cr + k) / (ca + k))
        score[cr < 10] = -9  # volume minimo para nao premiar ruido
        top = np.argsort(-score, kind='stable')[:10]
        alta[f] = [{'servico_id': str(sids[j]), 'recente': int(cr[j]), 'anterior': int(ca[j])} for j in top if score[j] > -9]
    json.dump(afin, open(f'{BASE}/afinidade_faixa.json', 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    json.dump(alta, open(f'{BASE}/em_alta_faixa.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print('faixas:', list(afin), '| periodo recente:', (mx - pd.Timedelta(days=27)).date(), 'a', mx.date())


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'validar'
    if cmd == 'checar':
        checar(sys.argv[2:])
    else:
        {'validar': validar, 'gerar': gerar, 'aux_api': aux_api}[cmd]()
