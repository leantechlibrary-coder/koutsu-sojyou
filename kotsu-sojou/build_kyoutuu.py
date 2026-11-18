"""case.json → 共通書式xlsx ＋ 証拠リスト（甲001〜、別紙の読み順で付番）
使い方: python build_kyoutuu.py case.json テンプレ.xlsx 出力.xlsx
"""
import json, sys, re, math, datetime as dt
from xlsx_patch import fill, Formula
import chien

SHEETS = ['事案の概要', '損害額一覧表', '治療費等集計表', '相続等一覧表（死亡事案で使用）']   # 読み順＝ブック内の並び
class Ev:                                                      # 証拠欄の仮置き
    def __init__(s, keys, text=None): s.keys, s.text = list(keys or []), text

import csv, os
def census(year):
    """女性・学歴計・全年齢平均（年額）。year は西暦。要確認の年は警告"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chingin_census_josei.csv')
    for r in csv.DictReader(open(path, encoding='utf-8')):
        if int(r['year']) == year:
            if r['verified'] == '要確認': print(f'警告: {r["wareki"]}の賃金センサスは要確認', file=sys.stderr)
            return int(r['josei_gakurekikei_zennenrei']), r['wareki']
    raise SystemExit(f'賃金センサス {year}年 の行がない')
def shufu(jiko_date):
    """事故前年のセンサスから (年額, 日額=円未満切捨て, 理由文)"""
    y, w = census(dt.date.fromisoformat(jiko_date).year - 1)
    return y, y // 365, f'{w}賃金センサス女性・学歴計・全年齢平均{y:,}円'

def d(s): return dt.date.fromisoformat(s) if s else None
def t(s): h, m = map(int, s.split(':')); return dt.time(h, m)

def akai(it, c):
    """慰謝料を赤い本で計算（千円未満切上げ）。表の選択・修正は judgment"""
    import isharyo
    if it['key'] == '通院慰謝料':
        tbl = it.get('table')
        if tbl not in ('I', 'II'): raise SystemExit('通院慰謝料の table に "I"（別表Ⅰ）か "II"（別表Ⅱ）を指定')
        nyu, tsu = isharyo.days_from_treatment(c['shogai']['treatment'], c['jiko']['date'])
        note = ''
        if it.get('hosei'):                      # 実通院日数×3（別表Ⅱ）／×3.5（別表Ⅰ）を上限に通院期間を短縮
            jitsu = sum((r[3] or 0) for r in c.get('receipts', []))
            cap = math.floor(jitsu * it['hosei'])
            if cap < tsu: note = f'、実通院{jitsu}日×{it["hosei"]}で通院期間{tsu}日を{cap}日に修正'; tsu = cap
        amt, how = isharyo.nyutsuin(nyu, tsu, tbl)
        name = '別表Ⅰ' if tbl == 'I' else '別表Ⅱ'
        return {'amount': amt, 'reason': it.get('reason') or f'赤い本{name}（入院{nyu}日、通院{tsu}日{note}、日割計算・千円未満切上げ）', 'calc': how}
    if it['key'] == '後遺障害慰謝料':
        g = it.get('grade')
        if g not in isharyo.KOUI: raise SystemExit('後遺障害慰謝料の grade に等級（1〜14）を指定')
        return {'amount': isharyo.koui(g), 'reason': it.get('reason') or f'赤い本 {g}級相当'}
    raise SystemExit(f'{it["key"]} は赤い本の自動計算に未対応')

BUSSON_FIX = {'車両修理費': 78, 'レッカー費用': 79, '代車費用': 80}
BUSSON_FREE = range(81, 89)      # 評価損・車両時価額・買替諸費用・休車損害・着衣等
HOTEN_FREE = range(95, 99)       # 車両保険金以外の既払（対物賠償の内払など）
SONOTA_FREE = range(101, 104)

def per_day(it):
    """{"daily":7500,"days":56} か {"daily":7500,"from":"…","to":"…"}（両端を含む）→ (金額, 説明)"""
    d = it['amount']
    days = d.get('days') or (dt.date.fromisoformat(d['to']) - dt.date.fromisoformat(d['from'])).days + 1
    span = f"{d['from']}〜{d['to']}の" if d.get('from') else ''
    return d['daily'] * days, f"{span}{days}日×日額{d['daily']:,}円"

def busson_cells(c, S):
    """物的損害（損害額一覧表 76〜105行）。金額は素材だけ入れ、小計・過失相殺・合計はExcelの数式に任せる"""
    b = c['busson']; items = b.get('items', [])
    keys = [i['key'] for i in items]
    if '車両修理費' in keys and '車両時価額' in keys:
        raise SystemExit('車両修理費と車両時価額は両立しない（分損か経済的全損かを決める）')
    for k, r in BUSSON_FIX.items():          # 使わない定型行は空にして非表示フィルタに乗せる
        if k not in keys: S[f'F{r}'] = '__CLEAR__'
    free = iter(BUSSON_FREE); subtotal = 0
    for it in items:
        amt, why = it['amount'], it.get('reason')
        if isinstance(amt, dict):
            amt, how = per_day(it); why = why or how
        if it['key'] in BUSSON_FIX: r = BUSSON_FIX[it['key']]
        else:
            r = next(free, None)
            if r is None: raise SystemExit('物損の損害項目は定型3行＋自由8行まで')
            S[f'E{r}'] = it['key']
        S.update({f'F{r}': amt, f'G{r}': why, f'H{r}': Ev(it.get('ev'))})
        subtotal += amt
    k = b.get('kashitsu')
    if k: S.update({'F91': k['rate'], 'G91': k.get('reason'), 'H91': Ev(k.get('ev'))})
    hfree = iter(HOTEN_FREE); hoten = 0
    for h in b.get('hoten', []):              # 既払・保険金はプラスで書く→マイナスで入れる
        r = 94 if h['key'] == '車両保険金' else next(hfree)
        if r != 94: S[f'E{r}'] = h['key']
        S.update({f'F{r}': -h['amount'], f'G{r}': h.get('reason'), f'H{r}': Ev(h.get('ev'))})
        hoten += h['amount']
    sfree = iter(SONOTA_FREE)
    for o in b.get('sonota', []):
        r = next(sfree); S.update({f'E{r}': o['key'], f'F{r}': o['amount'], f'G{r}': o.get('reason'), f'H{r}': Ev(o.get('ev'))})
    bf = b.get('bengoshi')
    if bf:
        if isinstance(bf.get('amount'), (int, float)): amt = bf['amount']
        else:                                   # 控除後の一定割合（既定10%、千円未満切捨て）。Excelと同じ式で控除後を出す
            sub = subtotal; after = sub - math.floor(sub * (k['rate'] if k else 0)) - hoten
            amt = math.floor(after * bf.get('rate', 0.1) / 1000) * 1000
            bf.setdefault('reason', f"控除後{after:,}円の{int(bf.get('rate', 0.1) * 100)}％（千円未満切捨て）")
        S.update({'F104': amt, 'G104': bf.get('reason')})

def jibai_info(c):
    """自賠責保険金の扱い → None（自賠責なし／元本から控除）か {'last': 最後の受領日, 'payments': [...]}（確定遅延損害金方式）"""
    for it in c.get('songai', {}).get('items', []):
        if it['key'] == '自賠責保険金' and it.get('method') == 'kakutei':
            pays = sorted((dict(p, date=d(p['date'])) for p in it['payments']), key=lambda p: p['date'])
            return {'last': pays[-1]['date'], 'payments': pays}
    return None

def jibai_cells(it, c, S):
    """自賠責保険金（損害額一覧表60行）。
    method "genpon"：受領額をそのまま元本から控除
    method "kakutei"：トリセツ第4の5(4)。受領日までの確定遅延損害金に先に充当し、
      金額等（F60）には確定遅延損害金を控除した残額、理由等（G60）には受領日・受領額・確定遅延損害金を書く。
      どちらも数式で書くので、損害額を直しても再計算される（日付から決まる部分だけ式に埋め込む）"""
    jiko = d(c['jiko']['date'])
    pays = sorted((dict(p, date=d(p['date'])) for p in it.get('payments', [])), key=lambda p: p['date'])
    if not pays: raise SystemExit('自賠責保険金は payments（受領日 date・受領額 amount・証拠 ev）で書く')
    if len(pays) > 4: raise SystemExit('自賠責保険金の受領は4回まで')
    total = sum(p['amount'] for p in pays)
    evs = list(dict.fromkeys(list(it.get('ev') or []) + [p['ev'] for p in pays if p.get('ev')]))
    method = it.get('method', 'genpon')
    if method == 'genpon':
        why = it.get('reason') or '、'.join(f"{chien.r_date(p['date'])}受領{p['amount']:,}円" for p in pays)
        S.update({'F60': -total, 'G60': why, 'H60': Ev(evs)})
        return
    if method != 'kakutei': raise SystemExit('自賠責保険金の method は "genpon" か "kakutei"')
    rate = chien.rate_for(jiko); te = it.get('truncate_each', True)
    segs = chien.segments(jiko, pays)
    ds, deduct = chien.chain_exprs('SUM(F55:F59)', rate, segs, te)
    S['F60'] = Formula(f'-({deduct})')
    hasu = '各区間円未満切捨て' if te else '東京地裁執行部方式の端数処理'
    tsudo = '受領の都度、' if len(segs) > 1 else ''
    head = f'{tsudo}確定遅延損害金（年{int(rate * 100)}％、初日算入、閏年366日、{hasu}）に充当し、残額を元本に充当。'
    parts = [f'"{head}"']
    for i, ((st, en, amt, sp), dx) in enumerate(zip(segs, ds)):
        no = '①②③④'[i] if len(segs) > 1 else ''
        frm = '事故日' if i == 0 else chien.r_date(st)
        parts.append(f'"{no}{chien.r_date(en)}受領{amt:,}円（{frm}〜{chien.r_date(en)}、{chien.days_text(sp)}）、確定遅延損害金"')
        parts.append(f'TEXT({dx},"#,##0")')
        parts.append('"円' + ('、' if i < len(segs) - 1 else '') + '"')
    S['G60'] = Formula('&'.join(parts), text=True)
    S['H60'] = Ev(evs)
    S['E60'] = '自賠責保険金'

def build_cells(c):
    A, S, T = {}, {}, {}
    ov = set()
    m, j = c['meta'], c['jiko']
    A.update({'D3': m['case_no'], 'D4': f"原告\u3000{m['plaintiff']}", 'D5': f"被告\u3000{m['defendant']}"})
    A.update({'E11': d(j['date']), 'F11': t(j['time']) if j.get('time') else None, 'I11': j['place'], 'N11': Ev(j['ev']),
              'E12': j['situation'], 'N12': Ev(j.get('situation_ev')), 'E23': j['mode'], 'N23': Ev(j.get('mode_ev'))})
    if len(j['vehicles']) > 4: raise SystemExit('関係車両は4台まで')
    for i, v in enumerate(j['vehicles']):
        r = 13 + 2 * i
        A.update({f'E{r}': v['side'], f'G{r}': v['type'], f'K{r}': v.get('reg'), f'G{r+1}': v.get('driver'),
                  f'K{r+1}': v.get('owner'), f'N{r}': Ev(v.get('ev'))})
    for i, s in enumerate(c['sekinin'][:4]):
        r = 27 + 2 * i
        A.update({f'D{r}': s['subject'], f'E{r}': s['basis'], f'G{r}': s.get('joint'), f'E{r+1}': s['fault'], f'N{r}': Ev(s.get('ev'))})
    g = c.get('shogai')
    if g:
        A.update({'E38': g['victim'], 'I38': d(g['dob']), 'N38': Ev(g.get('victim_ev')), 'I39': g['job'], 'N39': Ev(g.get('job_ev')),
                  'E40': g['injuries'], 'N40': Ev(g.get('injuries_ev'))})
        if len(g['treatment']) > 24: raise SystemExit('医療機関は24行まで')
        for i, tr in enumerate(g['treatment']):
            r = 42 + i
            A.update({f'E{r}': tr['hospital'], f'I{r}': tr['type'], f'K{r}': d(tr['end'])})
            if r == 42:
                ov.add(('事案の概要', 'K42'))
                if tr['start'] != j['date']: A['J42'] = d(tr['start']); ov.add(('事案の概要', 'J42'))
            else: A[f'J{r}'] = d(tr['start'])
        for i, f in enumerate(g.get('fixed', [])[:2]):
            A.update({f'E{69+i}': d(f['date']), f'F{69+i}': f.get('part')})
        if c.get('shibou'):                    # 見出しを「死亡日」にすると、損害額一覧表の基準日・年齢が死亡時になる
            A.update({'D69': '死亡日', 'E69': d(c['shibou']['date']), 'F69': None})
        for i, k in enumerate(g.get('kouisho', [])[:6]):
            r = 72 + i
            A.update({f'E{r}': k['part'], f'I{r}': k.get('jibaiseki'), f'K{r}': k.get('claim'), f'N{r}': Ev(k.get('ev'), k.get('ev_text'))})
        if g.get('kouisho_claim'): A.update({'F78': g['kouisho_claim'], 'N78': Ev(g.get('kouisho_claim_ev'))})

    if len(c.get('receipts', [])) > 96: raise SystemExit('レセプトは96行まで（1行1レセプト）')
    for i, (h, ym, nyu, tsu, amt, ev) in enumerate(c.get('receipts', [])):
        r = 8 + i
        T.update({f'D{r}': h, f'E{r}': ym, f'F{r}': nyu, f'G{r}': tsu, f'H{r}': amt, f'I{r}': Ev([ev])})
    for i, (h, one, ev) in enumerate(c.get('commute', [])[:10]):
        r = 111 + i; T.update({f'D{r}': h, f'E{r}': one, f'I{r}': Ev([ev])})

    so = c.get('songai', {})
    FIX = {'装具代': 8, '入院雑費': 9, '入院付添費': 10, '通院慰謝料': 44, '後遺障害慰謝料': 45,
           '任意保険金(治療費)': 56, '任意保険金(その他)': 57, '自賠責保険金': 60, '弁護士費用': 73,
           '死亡慰謝料': 46}
    LABEL = {'死亡慰謝料': '死亡慰謝料（本人分）'}
    death = bool(c.get('shibou'))
    if death and any(it['key'] == '弁護士費用' for it in so.get('items', [])):
        raise SystemExit('死亡事案の弁護士費用は shibou.bengoshi に原告ごとに書く（損害額一覧表F73に入れると相続額と二重になる）')
    MINUS = {'任意保険金(治療費)', '任意保険金(その他)', '自賠責保険金'}
    for it in so.get('items', []):
        if it['key'] == '自賠責保険金' and death: continue       # 死亡事案は相続等一覧表38行で原告ごとに
        if it['key'] == '自賠責保険金' and 'payments' in it:
            jibai_cells(it, c, S); continue
        r = FIX[it['key']]
        if it['key'] in LABEL: S[f'E{r}'] = LABEL[it['key']]
        if it.get('amount') == 'akai':
            it = dict(it); it.update(akai(it, c))
        S.update({f'F{r}': -it['amount'] if it['key'] in MINUS else it['amount'], f'G{r}': it.get('reason'), f'H{r}': Ev(it.get('ev'))})
    for k, cell in (('soin', 'F50'), ('kashitsu', 'F53')):
        if so.get(k): S[cell] = so[k]['rate']; S['G' + cell[1:]] = so[k].get('reason'); S['H' + cell[1:]] = Ev(so[k].get('ev'))
    for i, p in enumerate(so.get('kyugyo', [])[:3]):
        b = 116 + 5 * i
        if p['daily'] == 'shufu':
            _, p['daily'], why = shufu(j['date']); p.setdefault('daily_reason', why + '÷365（円未満切捨て）')
        S.update({f'G{b}': d(p['end']), f'F{b+1}': p['daily'], f'G{b+1}': p.get('daily_reason'), f'H{b+1}': Ev(p.get('daily_ev')),
                  f'F{b+3}': p['rate'], f'G{b+3}': p.get('rate_reason'), f'H{b+2}': Ev(p.get('count_ev'))})
        if p['count'] == '期間': S[f'E{b+2}'] = '休業期間'
        else: S[f'E{b+2}'] = '休業日数'; S[f'F{b+2}'] = p['count']; ov.add(('損害額一覧表', f'F{b+2}'))
    for i, p in enumerate(so.get('issitsu', [])[:3]):
        b = 134 + 9 * i
        if p['income'] == 'shufu':
            p['income'], _, why = shufu(j['date']); p.setdefault('income_reason', why)
        S.update({f'F{b+1}': p['income'], f'G{b+1}': p.get('income_reason'), f'H{b+1}': Ev(p.get('income_ev')),
                  f'F{b+2}': p['loss_rate'], f'G{b+2}': p.get('loss_reason'), f'F{b+3}': p.get('living', 0), f'F{b+5}': p['end_age']})
        S[f'G{b+3}'] = p.get('living_reason')
        if p.get('start_age') is not None:    # 年少者（18歳・22歳から）や、年金の期間を死亡時から重ねる場合
            S[f'F{b+4}'] = p['start_age']; S[f'G{b+4}'] = p.get('start_reason'); ov.add(('損害額一覧表', f'F{b+4}'))
    if c.get('busson'): busson_cells(c, S)
    U, wide = {}, False
    if death:
        import souzoku
        U, ov4, wide = souzoku.sheet4_cells(c, Ev, d, Formula); ov |= ov4
        tot = sum(it['amount'] for it in so.get('items', []) if it['key'] == '死亡慰謝料' and isinstance(it.get('amount'), int))
        tot += sum(sum(e.get('amounts', {}).values()) for e in c['shibou'].get('koyuu', []) if '慰謝料' in e['key'])
        print(f'参考: 死亡慰謝料（本人分＋近親者分）の合計 {tot:,}円。赤い本の目安は一家の支柱2800万円、母親・配偶者2500万円、その他2000万〜2500万円', file=sys.stderr)
    cells = {'事案の概要': A, '損害額一覧表': S, '治療費等集計表': T, '相続等一覧表（死亡事案で使用）': U}
    cells_meta['wide'] = wide
    return cells, ov
cells_meta = {}

PRESETS = {   # 事故記録の定型。Sonnetは kind と最小限の情報だけ書けばよい
    'bukken': dict(title='物件事故報告書', purpose='本件事故の発生日時、場所、態様等'),
    'jikkyo': dict(title='実況見分調書', purpose='本件事故の態様、現場の状況等'),
    'keiji':  dict(title='刑事記録', purpose='本件事故の態様、現場の状況等', date=''),
    'mitsumori': dict(title='見積書', purpose='原告車の修理費用の額'),
    'ryoshu':  dict(title='領収書', purpose='原告が原告車の修理費用を支払った事実及びその額'),
    'adjuster': dict(title='損害確認報告書', purpose='原告車の損傷の部位及び程度並びに修理費用の相当性'),
    'shashin': dict(title='写真', purpose='原告車の損傷の部位及び程度'),
    'jika':    dict(title='中古車価格資料', purpose='原告車の本件事故当時の時価額'),
    'daisha':  dict(title='代車料金の請求書', purpose='代車の使用期間及び費用'),
    'tow':     dict(title='レッカー代金の領収書', purpose='レッカー費用の額'),
}
def _r(iso):
    if not iso: return None
    d = dt.date.fromisoformat(iso); return f'R{d.year - 2018}.{d.month}.{d.day}'
def apply_presets(evmeta):
    for k, e in evmeta.items():
        pre = PRESETS.get(e.get('kind'))
        if not pre: continue
        for f, v in pre.items(): e.setdefault(f, v)
        if e['kind'] == 'shashin':        # 民訴規則148条：撮影対象・日時・場所を明らかにする
            e.setdefault('author', '原告')
            e.setdefault('note', '撮影対象：{}、撮影日：{}、撮影場所：{}'.format(
                e.get('taisho', '原告車'), _r(e.get('date')) or '[要確認]', e.get('basho') or '[要確認]'))
        if e['kind'] == 'keiji':          # 作成者＝実況見分調書の作成者を筆頭に「ほか」
            if not e.get('jikkyo_author'): e['author'] = '[要確認]実況見分調書の作成者'
            else: e['author'] = f"{e['jikkyo_author']}ほか"
            e['date'] = ''                 # 一式なので作成年月日は空欄

def number_evidence(data, evmeta, extra_order=()):
    apply_presets(evmeta)
    """別紙の読み順（シート順→行→列）で初出順に甲001〜を振り、Evを文字列に置き換える"""
    def key(ref): m = re.match(r'([A-Z]+)(\d+)', ref); return (int(m.group(2)), len(m.group(1)), m.group(1))
    order = []
    for sh in SHEETS:
        for ref in sorted(data[sh], key=key):
            v = data[sh][ref]
            if isinstance(v, Ev):
                for k in v.keys:
                    if k not in evmeta: raise SystemExit(f'証拠キー未定義: {k}')
                    if k not in order: order.append(k)
    for k in list(extra_order) + list(evmeta):         # 訴状本文だけに出る証拠 → 未使用証拠は警告
        if k not in order:
            if k in extra_order: order.append(k)
            else: print(f'警告: 別紙で使われていない証拠 {k}', file=sys.stderr)
    no = {k: f'甲{i+1:03d}' for i, k in enumerate(order)}
    seen = set()
    for sh in SHEETS:
        for ref in sorted(data[sh], key=key):
            v = data[sh][ref]
            if not isinstance(v, Ev): continue
            if v.text and not v.keys: data[sh][ref] = v.text; continue
            parts = []
            for k in v.keys:          # 事案の概要の初出だけ「番号:標目」、以後は番号のみ
                parts.append(f"{no[k]}:{evmeta[k]['title']}" if sh == '事案の概要' and k not in seen else no[k]); seen.add(k)
            s = '、'.join(parts) or None
            assert s is None or not re.search(r'[０-９]', s), '全角数字混入'
            data[sh][ref] = s
    return [dict(no=no[k], key=k, **evmeta[k]) for k in order]

if __name__ == '__main__':
    case = json.load(open(sys.argv[1], encoding='utf-8'))
    data, ov = build_cells(case)
    evlist = number_evidence(data, case['evidence'], case.get('sojou', {}).get('ev_order', []))
    for sh in data: data[sh] = {k: v for k, v in data[sh].items() if v is not None}
    fill(sys.argv[2], sys.argv[3], data, ov, unhide_cols={'相続等一覧表（死亡事案で使用）': (10, 13)} if cells_meta.get('wide') else None)
    json.dump(evlist, open(re.sub(r'\.xlsx$', '_証拠リスト.json', sys.argv[3]), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for e in evlist: print(e['no'], e['title'])
