"""公式テンプレのXMLを直接書き換えて値だけ入れる（入力規則・配列数式・保護を壊さない）"""
import zipfile, re, datetime, shutil
from lxml import etree
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
N={'m':NS}
def col2n(c):
    n=0
    for ch in c: n=n*26+ord(ch)-64
    return n
def split(ref):
    m=re.match(r'([A-Z]+)(\d+)$',ref); return m.group(1),int(m.group(2))
def serial(d):
    if isinstance(d,datetime.datetime): d=d.date()
    return (d-datetime.date(1899,12,30)).days
def sheet_paths(z):
    wb=etree.fromstring(z.read('xl/workbook.xml'))
    rels=etree.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    rmap={r.get('Id'):r.get('Target') for r in rels}
    out={}
    for s in wb.iter('{%s}sheet'%NS):
        rid=s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        out[s.get('name')]='xl/'+rmap[rid].lstrip('/').replace('xl/','')
    return out
class Formula(str):
    """数式として書き込む値（先頭の = は付けない）。キャッシュ値なしで書き、開いたときに再計算させる。
    結果が文字列の数式は Formula(式, text=True)"""
    def __new__(cls, s, text=False):
        o = str.__new__(cls, s); o.is_text = text; return o
def set_cell(root,ref,val,allow_formula_override=False):
    col,row=split(ref)
    sd=root.find('m:sheetData',N)
    r=sd.find(f'm:row[@r="{row}"]',N)
    if r is None: raise KeyError(f'row {row} not in template')
    c=r.find(f'm:c[@r="{ref}"]',N)
    if c is None:
        c=etree.SubElement(r,'{%s}c'%NS,r=ref)
        cells=sorted(r.findall('m:c',N),key=lambda e:col2n(split(e.get('r'))[0]))
        for e in cells: r.remove(e)
        for e in cells: r.append(e)
    if c.find('m:f',N) is not None and not allow_formula_override:
        raise ValueError(f'{ref} は数式セル（上書き禁止）')
    for ch in list(c): c.remove(ch)
    c.attrib.pop('t',None)
    if val is None or val == '__CLEAR__': return
    if isinstance(val,Formula):
        if len(val) > 8000: raise ValueError(f'{ref} の数式が長すぎる（{len(val)}文字）')
        if val.is_text: c.set('t','str')
        etree.SubElement(c,'{%s}f'%NS).text=str(val)
        return
    if isinstance(val,str):
        c.set('t','inlineStr'); is_=etree.SubElement(c,'{%s}is'%NS); t=etree.SubElement(is_,'{%s}t'%NS); t.text=val
        t.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
    elif isinstance(val,(datetime.date,datetime.datetime)):
        etree.SubElement(c,'{%s}v'%NS).text=str(serial(val))
    elif isinstance(val,datetime.time):
        etree.SubElement(c,'{%s}v'%NS).text=repr((val.hour*3600+val.minute*60)/86400)
    else:
        etree.SubElement(c,'{%s}v'%NS).text=repr(val)
def fill(template,out,data,overrides=(),unhide_cols=None):
    """data: {sheet:{ref:value}}  overrides: 上書きを許す数式セル {(sheet,ref)}
    unhide_cols: {sheet:(min,max)} 非表示の列を再表示する（相続等一覧表で原告5人以上のときのJ〜M列）"""
    zin=zipfile.ZipFile(template); paths=sheet_paths(zin)
    trees={}
    for sh,cells in data.items():
        if not cells and not (unhide_cols or {}).get(sh): continue
        p=paths[sh]; root=etree.fromstring(zin.read(p))
        for ref,v in cells.items(): set_cell(root,ref,v,(sh,ref) in overrides)
        rng=(unhide_cols or {}).get(sh)
        if rng:
            for col in root.iter('{%s}col'%NS):
                if col.get('min')==str(rng[0]) and col.get('max')==str(rng[1]): col.attrib.pop('hidden',None)
        trees[p]=root
    zout=zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED)
    for it in zin.infolist():
        b=zin.read(it.filename)
        if it.filename=='xl/calcChain.xml': continue
        if it.filename in trees: b=etree.tostring(trees[it.filename],xml_declaration=True,encoding='UTF-8',standalone=True)
        elif it.filename=='xl/workbook.xml': b=re.sub(rb'<calcPr([^/]*)/>',rb'<calcPr\1 fullCalcOnLoad="1"/>',b)
        elif it.filename=='[Content_Types].xml': b=re.sub(rb'<Override[^>]*calcChain[^>]*/>',b'',b)
        elif it.filename=='xl/_rels/workbook.xml.rels': b=re.sub(rb'<Relationship[^>]*calcChain[^>]*/>',b'',b)
        zout.writestr(it,b)
    zout.close()
