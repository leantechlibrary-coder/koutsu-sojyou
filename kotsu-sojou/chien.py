"""自賠責保険金受領までの確定遅延損害金（トリセツ第4の5(4)方式）

計算方法は LTL-Delay-Interest-Calc（calc_engine.py）の裁判所方式に合わせる：
  - 初日算入（事故日を含む）。受領日も含め、残元本の遅延損害金は受領日の翌日から
  - 端数期間暦年閏年説：元本×利率×（年数 ＋ 平年日数/365 ＋ 閏年日数/366）
  - 端数：各区間（年・平年・閏年）ごとに円未満切捨て（truncate_each=True、既定）
          False なら東京地裁執行部方式（各区間を小数第1位まで残して合算し、最後に切捨て）
  - 受領が複数回なら、受領の都度「確定遅延損害金に充当し、残りを元本に充当」して次の区間へ

金額はExcelの数式で出す（元本は損害額一覧表のセルを参照）。Pythonが決めるのは日付から決まる部分だけ。
"""
import calendar, datetime as dt, math

def rate_for(jiko):
    """事故日がR2.4.1前なら年5%、以後は年3%（共通書式F113と同じ境界）"""
    return 0.05 if jiko <= dt.date(2020, 3, 31) else 0.03

def _add_years(d, n):
    """relativedelta(years=n) と同じ（2/29 から平年へは 2/28）"""
    y = d.year + n
    day = min(d.day, calendar.monthrange(y, d.month)[1])
    return d.replace(year=y, day=day)

def split(start, end):
    """start〜end（両端を含む）→ (年数, 平年日数, 閏年日数, 総日数)"""
    total = (end - start).days + 1
    if total <= 0: raise ValueError(f'期間が0日以下: {start}〜{end}')
    years = 0
    while _add_years(start, years + 1) <= end: years += 1
    cur = _add_years(start, years)
    normal = leap = 0
    if cur < end:                       # calc_engine と同じく端数期間の開始日＝終了日は0日
        while cur <= end:
            seg_end = min(dt.date(cur.year, 12, 31), end)
            n = (seg_end - cur).days + 1
            if calendar.isleap(cur.year): leap += n
            else: normal += n
            if seg_end >= end: break
            cur = dt.date(cur.year + 1, 1, 1)
    return years, normal, leap, total

def segments(jiko, payments):
    """payments: [{'date': date, 'amount': int, ...}] → 区間 [(start, end, 金額, 分割)]"""
    out, start = [], jiko
    for p in sorted(payments, key=lambda p: p['date']):
        if p['date'] < start: raise SystemExit(f"自賠責の受領日 {p['date']} が前の区間より前")
        out.append((start, p['date'], p['amount'], split(start, p['date'])))
        start = p['date'] + dt.timedelta(days=1)
    return out

# ---- Python計算（検証・訴状用） ----
# 浮動小数点誤差を避けるため整数演算で行う（利率は 3/100 のような分数で持つ）。
# 例：1,137,049.4＋258,561.9＋12,426.7 は浮動小数点では 1,408,037.99… になり、切捨てで1円ずれる
def _ratio(rate):
    num = round(rate * 10000); return num, 10000

def _terms(base, rate, sp, scale=1):
    """各区間の floor(元本×利率×期間×scale)。整数だけで計算する"""
    y, n, l, _ = sp; num, den = _ratio(rate)
    return [base * num * y * scale // den, base * num * n * scale // (den * 365), base * num * l * scale // (den * 366)]

def interest(base, rate, sp, truncate_each=True):
    if truncate_each: return sum(_terms(base, rate, sp))
    return sum(_terms(base, rate, sp, 10)) // 10

def chain(base, rate, segs, truncate_each=True):
    """→ (各区間の確定遅延損害金, 元本から差し引く額の合計)"""
    ds, deduct = [], 0
    for _, _, amt, sp in segs:
        d = interest(base, rate, sp, truncate_each)
        ds.append(d); deduct += amt - d; base -= amt - d
    return ds, deduct

# ---- Excel数式 ----
# 元本は整数（共通書式はINTで円単位）。元本×分子×期間 を整数のまま作ってから1回だけ割るので、
# 切捨ての結果が浮動小数点誤差でずれない
def interest_expr(base, rate, sp, truncate_each=True):
    y, n, l, _ = sp
    num, den = _ratio(rate)
    g = math.gcd(num, den); num, den = num // g, den // g          # 3/100 の形にする
    k = 1 if truncate_each else 10
    terms = []
    if y: terms.append(f'INT(({base})*{num * y * k}/{den})')
    if n: terms.append(f'INT(({base})*{num * n * k}/{den * 365})')
    if l: terms.append(f'INT(({base})*{num * l * k}/{den * 366})')
    if not terms: return '0'
    return '+'.join(terms) if truncate_each else 'INT((' + '+'.join(terms) + ')/10)'

def chain_exprs(base, rate, segs, truncate_each=True):
    """→ (各区間の確定遅延損害金の式, 元本から差し引く額の合計の式)"""
    ds, deducts, b = [], [], base
    for _, _, amt, sp in segs:
        d = interest_expr(b, rate, sp, truncate_each)
        ds.append(d); deducts.append(f'({amt}-({d}))')
        b = f'{b}-({amt}-({d}))'
    return ds, '+'.join(deducts)

def r_date(d): return f'R{d.year - 2018}.{d.month}.{d.day}'

def days_text(sp):
    y, n, l, total = sp
    s = f'{total}日'
    if y or (n and l):
        inner = []
        if y: inner.append(f'{y}年')
        if n: inner.append(f'平年{n}日')
        if l: inner.append(f'閏年{l}日')
        s += '（' + '・'.join(inner) + '）'
    return s
