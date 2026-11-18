"""case.json（sojou欄）＋ office.json → 訴状docx ＋ mints入力メモ
使い方: python build_sojou.py case.json office.json 訴状.docx [--amount 人損合計] [--busson 物損合計] [--full]
  既定はmints用（第１ 請求の趣旨から。表紙・当事者目録なし）。--full で表紙・当事者目録つきの全体版
  合計額はWeb版Excelで再計算した共通書式の F74（人損）・F105（物損）を転記する。
"""
import json, sys, re, copy, zipfile, datetime as dt, argparse, math
from lxml import etree
import build_kyoutuu as bk

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W14 = 'http://schemas.microsoft.com/office/word/2010/wordml'
N = {'w': W, 'w14': W14}
q = lambda t: '{%s}%s' % (W, t)

ZEN = str.maketrans('0123456789-', '０１２３４５６７８９－')
def zen(s): return str(s).translate(ZEN)
def wareki(d):
    if isinstance(d, str): d = dt.date.fromisoformat(d)
    y = d.year - 2018
    return zen(f'令和{"元" if y == 1 else y}年{d.month}月{d.day}日')
def yen(n):
    """40142986 → ４０１４万２９８６円（億・万で区切る）"""
    oku, r = divmod(n, 10**8); man, ichi = divmod(r, 10**4)
    s = (f'{oku}億' if oku else '') + (f'{man}万' if man else '') + (f'{ichi}' if ichi else '')
    return zen(s or '0') + '円'
def inshi(v):
    """民事訴訟費用等に関する法律 別表第一の一項（訴えの提起）"""
    steps = [(100e4, 10e4, 1000), (500e4, 20e4, 1000), (1000e4, 50e4, 2000),
             (10e8, 100e4, 3000), (50e8, 500e4, 10000), (float('inf'), 1000e4, 10000)]
    fee, lo = 0, 0
    for hi, unit, add in steps:
        if v > lo: fee += math.ceil((min(v, hi) - lo) / unit) * add
        lo = hi
    return int(fee)
def rate(jiko):  # 改正民法施行日（R2.4.1）前の事故は年５分
    return (5 if dt.date.fromisoformat(jiko) < dt.date(2020, 4, 1) else 3)

def ptext(p): return ''.join(t.text or '' for t in p.iter(q('t')))
def set_text(p, text):
    """段落の文字を差し替える（最初のrunの書式を残す）"""
    runs = [r for r in p if r.tag == q('r')]
    keep = runs[0] if runs else etree.SubElement(p, q('r'))
    for r in runs[1:]: p.remove(r)
    for ch in list(keep):
        if ch.tag != q('rPr'): keep.remove(ch)
    t = etree.SubElement(keep, q('t')); t.text = text
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
def find(body, pat):
    for p in body.iter(q('p')):
        if re.search(pat, ptext(p)): return p
    raise KeyError(f'テンプレに段落がない: {pat}')
GOTHIC = 'ＭＳ ゴシック'
def set_check(sdt, on):
    """チェック記号をWingdingsでなくUnicodeの☒/☐にする（Word以外でPDF化しても化けない）"""
    sdt.find('.//w14:checked', N).set('{%s}val' % W14, '1' if on else '0')
    cs = sdt.find('.//w14:checkedState', N); cs.set('{%s}val' % W14, '2612'); cs.set('{%s}font' % W14, GOTHIC)
    r = sdt.find('w:sdtContent/w:r', N)
    for ch in list(r): r.remove(ch)
    rpr = etree.SubElement(r, q('rPr')); f = etree.SubElement(rpr, q('rFonts'))
    for a in ('ascii', 'eastAsia', 'hAnsi'): f.set(q(a), GOTHIC)
    t = etree.SubElement(r, q('t')); t.text = '☒' if on else '☐'
def is_on(sdt): return sdt.find('.//w14:checked', N).get('{%s}val' % W14) == '1'
def para_like(tmpl, text):
    p = copy.deepcopy(tmpl)
    if text == '': [p.remove(r) for r in p if r.tag == q('r')]
    else: set_text(p, text)
    return p


def make_claims(case, amount, busson):
    """原告ごとの請求額と遅延損害金の起算日。
    死亡事案：相続等一覧表の数式をPythonで再現（amount＝損害額一覧表F74、busson＝F105）。
    確定遅延損害金方式：弁護士費用（と物損）は事故日から、残りは最後の自賠責受領日の翌日から"""
    sj = case['sojou']
    if case.get('shibou'):
        import souzoku
        rows = souzoku.mirror(case, amount, busson or 0)
        it = souzoku.jibai_item(case)
        npay = len(it.get('payments', [])) if it else 0
        pls = {p['name'] for p in sj.get('plaintiffs', [])}
        for x in rows:
            if x['name'] not in pls: raise SystemExit(f'sojou.plaintiffs に {x["name"]} がない（shibou.heirs と氏名を揃える）')
        lab = '弁護士費用' + ('及び物的損害の相続額' if busson else '')
        return [dict(who=f"原告{x['name']}", row=x, total=x['total'], early=x['early'], late=x['late'],
                     late_from=x['late_from'], early_label=lab, npay=npay) for x in rows]
    total = amount + (busson or 0)
    jb = bk.jibai_info(case)
    if not jb: return [dict(who='原告', total=total, early=total, late=0, late_from=None)]
    bengo = next((it['amount'] for it in case.get('songai', {}).get('items', []) if it['key'] == '弁護士費用'), 0)
    if not isinstance(bengo, int): raise SystemExit('確定遅延損害金方式では弁護士費用を金額（整数）で書く')
    if amount - bengo <= 0: raise SystemExit('人損合計から弁護士費用を引くと0以下になる（--amount を確認）')
    return [dict(who='原告', total=total, early=bengo + (busson or 0), late=amount - bengo,
                 late_from=jb['last'] + dt.timedelta(days=1), early_label='弁護士費用' + ('及び物的損害' if busson else ''),
                 npay=len(jb['payments']))]

def kisan(cl, jiko, paren=False):
    """請求の趣旨（paren=False）・よって書き（paren=True）の「及び…から支払済みまで」の部分"""
    if not cl['late_from']:
        return f'及びこれに対する{"不法行為の日である" if paren else ""}{wareki(jiko)}から支払済みまで'
    p2 = (('最後の' if cl.get('npay', 1) > 1 else '') + '自賠責保険金受領日の翌日である') if paren else ''
    if not cl['early']:
        return f'及びこれに対する{p2}{wareki(cl["late_from"])}から支払済みまで'
    a = f'（{cl["early_label"]}）' if paren else ''
    b = '（自賠責保険金を確定遅延損害金及び元本に充当した後の残額）' if paren else ''
    p1 = '不法行為の日である' if paren else ''
    return f'及びうち{yen(cl["early"])}{a}に対する{p1}{wareki(jiko)}から、うち{yen(cl["late"])}{b}に対する{p2}{wareki(cl["late_from"])}から各支払済みまで'

def memo_check(claims, case, amount, busson, r, jiko):
    out = []
    if case.get('shibou'):
        out.append(f'- 損害額一覧表 F74（本人分）{amount:,}円' + (f'、F105 {busson:,}円' if busson else '') + ' から、相続等一覧表を再現した額。Web版Excelの「請求額合計」（45行＋物損70行）と一致するか照合する：')
        out.append('')
        out.append('| 原告 | 列 | 相続額 | 自賠責（38行） | 控除後（43行） | 弁護士費用（44行） | 請求額 |')
        out.append('|---|---|---|---|---|---|---|')
        for cl in claims:
            x = cl['row']
            out.append(f"| {x['name']} | {x['col']} | {x['souzoku']:,} | {x['jibai']:,} | {x['f43']:,} | {x['bengoshi']:,} | {x['total']:,} |")
        out.append('')
        out.append('- 遺族補償年金・遺族年金の控除額が、その原告の逸失利益の相続分（損害額一覧表の逸失利益×(1−素因減額率)×(1−過失相殺率)×相続分）を超えていないか（費目拘束）')
    for cl in claims:
        if cl['late_from'] and cl['early']:
            out.append(f"- {cl['who']}：遅延損害金 年{r}％。{cl['early']:,}円（{cl['early_label']}）は事故日 {jiko}から、{cl['late']:,}円は自賠責受領日（複数回なら最後）の翌日 {cl['late_from']}から")
        elif cl['late_from']:
            out.append(f"- {cl['who']}：遅延損害金 年{r}％。{cl['total']:,}円を自賠責受領日（複数回なら最後）の翌日 {cl['late_from']}から")
        else:
            out.append(f"- {cl['who']}：遅延損害金 年{r}％。{cl['total']:,}円を事故日 {jiko}から")
    return '\n'.join(out)

def build(case, office, amount, busson, out, template, full=False):
    sj = case['sojou']; jiko = case['jiko']['date']
    # 証拠番号（共通書式と同じ規則で振る）
    data, _ = bk.build_cells(case)
    evlist = bk.number_evidence(data, case['evidence'], sj.get('ev_order', []))
    no = {e['key']: e['no'] for e in evlist}
    def ev(s):
        def rep(m):
            if m.group(1) not in no: raise SystemExit(f'訴状本文の証拠キー未定義: {m.group(1)}')
            return no[m.group(1)]
        return re.sub(r'\{証:([a-z0-9_A-Z]+)\}', rep, s)

    defs = [s for s in case['sekinin']]
    multi = len({s['subject'] for s in defs}) > 1
    hi = '被告ら' if multi else '被告'
    r = rate(jiko)
    claims = make_claims(case, amount, busson)
    total = sum(cl['total'] for cl in claims)

    z = zipfile.ZipFile(template)
    root = etree.fromstring(z.read('word/document.xml')); body = root.find('w:body', N)

    for sdt in body.iter(q('sdt')):
        if sdt.find('.//w14:checkbox', N) is not None: set_check(sdt, is_on(sdt))
    set_text(find(body, r'^令和○年○月○日$'), wareki(sj['date']))
    set_text(find(body, r'地方裁判所'), f"{sj['court']}\u3000御中")
    seal = '\u3000印' if office.get('seal_mark', False) else ''
    set_text(find(body, r'^原告訴訟代理人弁護士'), f"{'原告ら' if len(claims) > 1 else '原告'}訴訟代理人弁護士\u3000{office['lawyer_spaced']}{seal}")
    set_text(find(body, r'^当事者の表示'), '当事者の表示\u3000別紙当事者目録記載のとおり')
    set_text(find(body, r'^訴訟物の価額'), f'訴訟物の価額\u3000{yen(total)}')
    set_text(find(body, r'^貼用印紙額'), f'貼用印紙額\u3000\u3000{yen(inshi(total))}')
    rentai = '連帯して、' if multi else ''
    p1 = find(body, r'^１\u3000被告らは'); anchor = p1
    for i, cl in enumerate(claims):
        text = f'{zen(i+1)}\u3000{hi}は、{cl["who"]}に対し、{rentai}{yen(cl["total"])}{kisan(cl, jiko)}年{zen(r)}パーセントの割合による金員を支払え。'
        if i == 0: set_text(p1, text)
        else: p = para_like(p1, text); anchor.addnext(p); anchor = p
    set_text(find(body, r'^２\u3000訴訟費用は'), f'{zen(len(claims)+1)}\u3000訴訟費用は{hi}の負担とする。')

    # 請求の原因１：チェックボックス（治療費等集計表・相続等一覧表）
    p1 = find(body, r'^１\u3000別紙「事案の概要」')
    boxes = p1.findall('w:sdt', N)
    set_check(boxes[0], bool(case.get('receipts')))
    set_check(boxes[1], bool(sj.get('souzoku') or case.get('shibou')))
    # 請求の原因２：補足主張
    p2 = find(body, r'^２\u3000事前交渉の経過')
    h_tmpl, b_tmpl = find(body, r'^\(1\) 事故態様'), find(body, r'^・・・$')
    olds = [h_tmpl, b_tmpl, find(body, r'^\(2\) 後遺障害')]
    olds.append(olds[-1].getnext())
    hosoku = sj.get('hosoku', [])
    p3 = find(body, r'^３\u3000よって')
    if hosoku or sj.get('shucho_ichiran'):
        b2 = p2.findall('w:sdt', N)
        set_check(b2[0], bool(hosoku)); set_check(b2[1], bool(sj.get('shucho_ichiran')))
        anchor = olds[-1]
        for i, h in enumerate(hosoku):
            anchor.addprevious(para_like(h_tmpl, f'({i+1}) {h["title"]}'))
            for line in h['body']: anchor.addprevious(para_like(b_tmpl, ev(line)))
        num3 = '３'
    else:
        body.remove(p2); num3 = '２'
    for p in olds: body.remove(p)
    # 請求の原因３：よって書き
    subj = '原告ら' if len(claims) > 1 else '原告'
    if multi:
        per = '、'.join(f"{s['subject']}に対しては{s['basis']}{'及び' + s['joint'] if s.get('joint') else ''}に基づき" for s in defs)
        yotte = f'{subj}は、{per}、連帯して、'
    else:
        s = defs[0]; yotte = f"{subj}は、被告に対し、{s['basis']}{'及び' + s['joint'] if s.get('joint') else ''}に基づき、"
    tail = f'民法所定の年{zen(r)}分の割合による遅延損害金'
    if len(claims) == 1:
        cl = claims[0]; mot = f'損害賠償金{yen(cl["total"])}{kisan(cl, jiko, True)}{tail}の支払を求める。'
    else:
        mot = '損害賠償金として、' + '、'.join(f'{cl["who"]}につき{yen(cl["total"])}{kisan(cl, jiko, True)}{tail}' for cl in claims) + 'の支払を求める。'
    set_text(p3, f'{num3}\u3000よって、{yotte}{mot}')

    # 第３ 本訴に至る経緯
    k1 = find(body, r'^１\u3000本件事故後'); k_tmpl = k1
    olds = [k1, find(body, r'^２\u3000被告側からは'), find(body, r'^なお、本訴についても')]
    for i, line in enumerate(sj.get('keii', [])):
        olds[0].addprevious(para_like(k_tmpl, f'{zen(i+1)}\u3000{ev(line)}'))
    for p in olds: body.remove(p)
    set_text(find(body, r'^証拠方法'), f"証拠方法\u3000{wareki(sj['date'])}付け証拠説明書記載のとおり")
    fz = sj.get('fuzoku', ['証拠説明書\u3000１通', '甲号証\u3000各１通', '訴訟委任状\u3000１通'])
    if 'fuzoku' not in sj and any(p.get('daiinin') for p in sj.get('plaintiffs', [])):
        fz = fz + ['戸籍謄本（法定代理権を証する書面）\u3000１通']
    set_text(find(body, r'^附属書類'), '附属書類\u3000' + fz[0])
    pf = find(body, r'^附属書類')
    for line in reversed(fz[1:]): pf.addnext(para_like(pf, '\u3000\u3000\u3000\u3000\u3000' + line))

    # 当事者目録（別紙）：全体版のみ。mintsでは当事者はフォームに入力し申立書に出る
    ijo = find(body, r'^以上$'); sect = body.find('w:sectPr', N)
    plain = find(body, r'^損害賠償請求事件$')
    def add(text, center=False, br=False):
        p = para_like(plain, text)
        if center:
            ppr = p.find('w:pPr', N); jc = etree.SubElement(ppr, q('jc')); jc.set(q('val'), 'center')
        if br:
            rr = etree.Element(q('r')); b = etree.SubElement(rr, q('br')); b.set(q('type'), 'page'); p.insert(1, rr)
        sect.addprevious(p)
    if full:
        add('（別紙）', br=True)
        for pl in (sj.get('plaintiffs') or [sj['plaintiff']]):
            add(f"〒{zen(pl['zip'])}\u3000{pl['address']}"); add(f"原告\u3000\u3000\u3000\u3000\u3000\u3000\u3000{pl['name']}")
            if pl.get('daiinin'): add(pl['daiinin'])
            add('')
        add(f"〒{zen(office['zip'])}\u3000{office['address']}（送達場所）"); add(office['office_name'])
        add(f"電話\u3000{zen(office['tel'])}"); add(f"ＦＡＸ\u3000{zen(office['fax'])}")
        add(f"上記{'原告ら' if len(claims) > 1 else '原告'}訴訟代理人弁護士\u3000{office['lawyer_spaced']}"); add('')
        for d in sj['defendants']:
            add(f"〒{zen(d['zip'])}\u3000{d['address']}"); add(f"被告\u3000\u3000\u3000\u3000\u3000\u3000\u3000{d['name']}"); add('')
        pe = para_like(ijo, '以上'); sect.addprevious(pe)

    if not full:
        # mints用：表紙部分（表題〜貼用印紙額）を削り「第１ 請求の趣旨」から始める
        first = find(body, r'^第１\u3000請求の趣旨')
        for p in list(body):
            if p is first: break
            if p.tag == q('p'): body.remove(p)
    xml = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    zo = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
    for it in z.infolist():
        b = xml if it.filename == 'word/document.xml' else z.read(it.filename)
        if re.match(r'word/header\d+\.xml$', it.filename) and '記載例' in re.sub(r'<[^>]+>', '', b.decode('utf-8')):
            b = re.sub(rb'(<w:hdr[^>]*>).*(</w:hdr>)', rb'\1<w:p/>\2', b, flags=re.S)
        if it.filename == 'docProps/core.xml':   # 作成者情報を消す
            b = re.sub(rb'<dc:creator>.*?</dc:creator>', b'<dc:creator></dc:creator>', b)
            b = re.sub(rb'<cp:lastModifiedBy>.*?</cp:lastModifiedBy>', b'<cp:lastModifiedBy></cp:lastModifiedBy>', b)
        zo.writestr(it, b)
    zo.close()

    man = math.ceil(total / 10000)
    shoko = memo_check(claims, case, amount, busson, r, jiko)
    memo = f"""# mints 新規申立て入力メモ

| 項目 | 入力内容 |
|---|---|
| 申立種別 | 訴え提起 |
| 提出先 | {sj['court']} |
| 事件名 | 損害賠償請求事件 |
| 原告の数 | {len(claims)} |\n| 被告の数 | {len(sj['defendants'])} |
| 訴額（万円・切上げ） | {man} |
| 手数料（照合用） | {inshi(total):,}円 ※mintsの自動計算と一致するか確認 |
| 申立ての趣旨 | 別紙請求の趣旨記載のとおり |
| 申立ての理由 | 別紙請求の原因記載のとおり |

## 添付・提出ファイル
- 訴状（PDF・添付書類。第１ 請求の趣旨から始まるmints用。表紙・当事者目録はフォームから作られる申立書が担う）
- 共通書式（PDF・正式）／共通書式（xlsx・記録外）
- 甲001〜{evlist[-1]['no']}（PDF・証拠）／証拠説明書（PDF）
- ファイル名は拡張子込み100文字以内

## 確認
- 請求額の合計 {total:,}円＝共通書式の合計と一致しているか
{shoko}
"""
    open(re.sub(r'\.docx$', '_mints入力メモ.md', out), 'w', encoding='utf-8').write(memo)
    return evlist

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('case'); ap.add_argument('office'); ap.add_argument('out')
    ap.add_argument('--amount', type=int); ap.add_argument('--busson', type=int)
    ap.add_argument('--template', default=None)
    ap.add_argument('--full', action='store_true', help='表紙・当事者目録つきの全体版（紙・旧方式用）')
    a = ap.parse_args()
    case = json.load(open(a.case, encoding='utf-8')); office = json.load(open(a.office, encoding='utf-8'))
    amount = a.amount if a.amount is not None else case['sojou'].get('amount', 0)
    busson = a.busson if a.busson is not None else case['sojou'].get('busson', 0)
    if not (amount or busson): raise SystemExit('請求額を指定する：人損合計（F74）は --amount、物損合計（F105）は --busson（または sojou.amount／sojou.busson）')
    import os
    tpl = a.template or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', '202602sojou_songaibaishou_itiranhyouriyou.docx')
    build(case, office, amount, busson, a.out, tpl, a.full)
    print('出力:', a.out)
