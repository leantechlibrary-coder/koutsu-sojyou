"""死亡事案：相続等一覧表（sheet4）への書き込みと、訴状用の検算

構造（共通書式の流れのまま。行の追加・削除はしない）
  損害額一覧表 = 被相続人本人の損害。過失相殺・任意保険金等の控除まで。自賠責（F60）と弁護士費用（F73）は0
  相続等一覧表 = F74 を相続分で按分（23行）＋固有の損害（24〜27行、素因減額・過失相殺は30・33行）
                 填補 38行：自賠責保険金（相続分で割付け。確定遅延損害金方式ならトリセツ第4の5(4)の残額）
                      39〜41行：労災・公的年金（受給者の列だけ。事故日に元本から控除＝最大判平27.3.4）
                 弁護士費用 44行（原告ごと）
金額は数式で入れる（相続等一覧表の数式が計算する）。Pythonの再現（mirror）は訴状の金額と照合用。
"""
import datetime as dt, math
from fractions import Fraction
import chien

SHEET = '相続等一覧表（死亡事案で使用）'
COLS = 'FGHIJKLM'                   # 原告は8人まで（J〜Mは非表示列。5人以上なら再表示する）
KOYUU_ROWS = range(24, 28)          # 固有の損害は4項目まで
HOTEN_ROWS = range(39, 42)          # 原告ごとの填補（自賠責以外）は3項目まで
KINDS = {'isshitsu': '逸失利益', 'sougi': '葬儀費用'}   # 費目拘束：どの損害からだけ控除できるか

def share(h):
    s = h.get('share')
    if s in (None, '', 0): return Fraction(0)
    return Fraction(s) if isinstance(s, str) else Fraction(s).limit_denominator(1000)

def heirs(c):
    hs = c['shibou']['heirs']
    if len(hs) > 8: raise SystemExit('原告（相続人・近親者）は8人まで')
    names = [h['name'] for h in hs]
    if len(set(names)) != len(names): raise SystemExit('原告の氏名が重複している')
    total = sum(share(h) for h in hs)
    if total > 1: raise SystemExit(f'相続分の合計が1を超える（{total}）')
    return hs

def per_heir(entry, hs, what):
    """{"氏名": 金額} → 列順のリスト。知らない氏名は止める"""
    amts = entry.get('amounts', {})
    names = {h['name'] for h in hs}
    for n in amts:
        if n not in names: raise SystemExit(f'{what}「{entry.get("key")}」の氏名 {n} が heirs にない')
    return [amts.get(h['name'], 0) or 0 for h in hs]

def jibai_item(c):
    return next((it for it in c.get('songai', {}).get('items', []) if it['key'] == '自賠責保険金'), None)

def payments(it, d):
    pays = sorted((dict(p, date=d(p['date'])) for p in it.get('payments', [])), key=lambda p: p['date'])
    if not pays: raise SystemExit('自賠責保険金は payments（受領日 date・受領額 amount・証拠 ev）で書く')
    if len(pays) > 4: raise SystemExit('自賠責保険金の受領は4回まで')
    return pays

def split_pays(pays, sh):
    """受領額を相続分で割り付ける（円未満切捨て）"""
    return [dict(p, amount=math.floor(p['amount'] * sh)) for p in pays]

def bengoshi_expr(b, col, amt=None):
    if isinstance(amt, int): return amt
    pct = round(b.get('rate', 0.1) * 100)
    return f'INT({col}43*{pct}/100000)*1000'          # 控除後の pct％、千円未満切捨て（浮動小数点を避けた形）

def sheet4_cells(c, Ev, d, Formula):
    """→ (相続等一覧表のセル, 上書きを許す数式セル, 5人以上か)"""
    sb = c['shibou']; hs = heirs(c); so = c.get('songai', {})
    U, ov = {}, set()
    jiko = d(c['jiko']['date'])
    for i, h in enumerate(hs):
        r = 5 + i
        U.update({f'E{r}': h['name'], f'F{r}': h.get('zokugara'), f'G{r}': float(share(h)) if share(h) else None})
    U['Q22'] = Ev(sb.get('heirs_ev'))                   # 相続関係の証拠（戸籍等）

    # 固有の損害（24〜27行）
    ky = sb.get('koyuu', [])
    if len(ky) > 4: raise SystemExit('固有の損害は4項目まで')
    for r, e in zip(KOYUU_ROWS, ky):
        U[f'E{r}'] = e['key']
        for col, v in zip(COLS, per_heir(e, hs, '固有の損害')):
            if v: U[f'{col}{r}'] = v
        U.update({f'P{r}': e.get('reason'), f'Q{r}': Ev(e.get('ev'))})
    for k, cell in (('soin', 'F30'), ('kashitsu', 'F33')):     # 固有分にも本人と同じ率（判断が分かれるなら judgment）
        if so.get(k): U[cell] = so[k]['rate']

    # 原告ごとの填補（39〜41行）：労災・公的年金。受給者の列だけ、マイナスで
    ht = sb.get('hoten', [])
    if len(ht) > 3: raise SystemExit('原告ごとの填補（自賠責以外）は3項目まで')
    for r, e in zip(HOTEN_ROWS, ht):
        if e.get('kind') not in KINDS: raise SystemExit(f'填補「{e["key"]}」の kind は isshitsu（逸失利益から控除）か sougi（葬儀費用から控除）')
        U[f'E{r}'] = e['key']
        for col, v in zip(COLS, per_heir(e, hs, '填補')):
            if v: U[f'{col}{r}'] = -v
        U.update({f'P{r}': e.get('reason') or f'{KINDS[e["kind"]]}から控除（受給者のみ、支給済み・支給確定分）', 'Q' + str(r): Ev(e.get('ev'))})

    # 自賠責保険金（38行）
    it = jibai_item(c)
    if it:
        pays = payments(it, d); total = sum(p['amount'] for p in pays)
        evs = list(dict.fromkeys(list(it.get('ev') or []) + [p['ev'] for p in pays if p.get('ev')]))
        method = it.get('method', 'genpon')
        lst = '、'.join(f"{chien.r_date(p['date'])}受領{p['amount']:,}円" for p in pays)
        U['E38'] = '自賠責保険金'; U['Q38'] = Ev(evs)
        if method == 'genpon':
            for col, h in zip(COLS, hs):
                if share(h): U[f'{col}38'] = -math.floor(total * share(h))
            U['P38'] = it.get('reason') or f'自賠責保険金（{lst}）を相続分により割り付けて控除'
        elif method == 'kakutei':
            rate = chien.rate_for(jiko); te = it.get('truncate_each', True)
            hasu = '各区間円未満切捨て' if te else '東京地裁執行部方式の端数処理'
            tsudo = '受領の都度、' if len(pays) > 1 else ''
            segs0 = chien.segments(jiko, pays)
            kikan = '、'.join(f"{'事故日' if i == 0 else chien.r_date(s)}〜{chien.r_date(e)}（{chien.days_text(sp)}）"
                            for i, (s, e, _, sp) in enumerate(segs0))
            parts = [f'"自賠責保険金（{lst}）を相続分により各原告に割り付け、{tsudo}確定遅延損害金（年{int(rate * 100)}％、初日算入、閏年366日、{hasu}）に充当し、残額を元本に充当。期間：{kikan}。確定遅延損害金："']
            first = True
            for col, h in zip(COLS, hs):
                if not share(h): continue
                segs = chien.segments(jiko, split_pays(pays, share(h)))
                base = f'{col}23+{col}35+SUM({col}39:{col}41)'     # 原告の元本＝相続額＋固有の損害（過失相殺後）−労災等
                ds, deduct = chien.chain_exprs(base, rate, segs, te)
                U[f'{col}38'] = Formula(f'-({deduct})')
                parts.append(f'"{"" if first else "、"}{h["name"]} "&TEXT({"+".join(f"({x})" for x in ds)},"#,##0")&"円"')
                first = False
            U['P38'] = Formula('&'.join(parts), text=True)
        else: raise SystemExit('自賠責保険金の method は "genpon" か "kakutei"')

    # 弁護士費用（44行）
    b = sb.get('bengoshi', {'rate': 0.1})
    amts = b.get('amounts', {})
    for col, h in zip(COLS, hs):
        v = bengoshi_expr(b, col, amts.get(h['name']))
        U[f'{col}44'] = Formula(v) if isinstance(v, str) else v
    U['P44'] = b.get('reason') or ('原告ごとに定額' if amts else f"控除後の{round(b.get('rate', 0.1) * 100)}％（千円未満切捨て）")
    return U, ov, len(hs) > 4

# ---- 検算（相続等一覧表の数式をPythonで再現。訴状の金額と、Excelで見る請求額の照合用） ----
def mirror(c, f74, f105=0):
    from build_kyoutuu import d
    sb = c['shibou']; hs = heirs(c); so = c.get('songai', {})
    jiko = d(c['jiko']['date'])
    soin = Fraction(str(so['soin']['rate'])) if so.get('soin') else 0
    kash = Fraction(str(so['kashitsu']['rate'])) if so.get('kashitsu') else 0
    ky = [per_heir(e, hs, '固有の損害') for e in sb.get('koyuu', [])]
    ht = [(e, per_heir(e, hs, '填補')) for e in sb.get('hoten', [])]
    it = jibai_item(c)
    pays = payments(it, d) if it else []
    method = it.get('method', 'genpon') if it else None
    b = sb.get('bengoshi', {'rate': 0.1}); bam = b.get('amounts', {})
    out = []
    for i, h in enumerate(hs):
        sh = share(h)
        souzoku = math.floor(f74 * sh)                       # F23 = INT($D18*F22)
        k = sum(row[i] for row in ky)                        # F29
        k_soin = k - math.floor(k * soin)                    # F32 = F29 - INT(F29*F30)
        k_after = k_soin - math.floor(k_soin * kash)         # F35
        hoten = -sum(v[i] for _, v in ht)                    # 39〜41行（マイナス）
        base = souzoku + k_after + hoten
        jb, ds = 0, []
        if pays and sh:
            if method == 'kakutei':
                segs = chien.segments(jiko, split_pays(pays, sh))
                ds, ded = chien.chain(base, chien.rate_for(jiko), segs, it.get('truncate_each', True))
                jb = -ded
            else: jb = -math.floor(sum(p['amount'] for p in pays) * sh)
        f43 = souzoku + k_after + jb + hoten
        bg = bam.get(h['name']) if isinstance(bam.get(h['name']), int) else (f43 * round(b.get('rate', 0.1) * 100) // 100000) * 1000
        f45 = f43 + bg
        f70 = math.floor(f105 * sh)                          # 物損は相続額のみ（弁護士費用は F105 に含める）
        late_from = (pays[-1]['date'] + dt.timedelta(days=1)) if (method == 'kakutei' and sh and pays) else None
        early = bg + f70 if late_from else f45 + f70
        late = f43 if late_from else 0
        # 費目拘束のチェック（葬祭料 ≦ その原告の葬儀費用）
        for (e, v) in ht:
            if e['kind'] == 'sougi' and v[i]:
                fu = next((row[i] for ee, row in zip(sb.get('koyuu', []), ky) if '葬儀' in ee['key']), 0)
                if v[i] > fu: raise SystemExit(f'{h["name"]}：葬祭料 {v[i]:,}円 が葬儀費用 {fu:,}円 を超える（費目拘束）')
        out.append(dict(name=h['name'], col=COLS[i], share=sh, souzoku=souzoku, koyuu=k_after, hoten=hoten, jibai=jb,
                        chien=ds, f43=f43, bengoshi=bg, f45=f45, f70=f70, total=f45 + f70,
                        early=early, late=late, late_from=late_from))
    return out

SHIBOU_ISHARYO = {'一家の支柱': 28000000, '母親・配偶者': 25000000}   # 赤い本：本人分と近親者分の合計の目安（その他は2000万〜2500万円）
