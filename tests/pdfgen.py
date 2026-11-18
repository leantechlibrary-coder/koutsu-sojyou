"""テスト用の合成PDF。実物の書式に近い位置に文字を置き、スキャナのOCRの癖（字間の空白・全角数字）も混ぜられる。"""
from pathlib import Path

import pymupdf


def make_pdf(path: Path, pages, fontsize=10):
    """pages: [[(x, y, 文字), ...], ...]。y は上から（ポイント）。"""
    doc = pymupdf.open()
    for items in pages:
        pg = doc.new_page(width=595, height=842)
        for x, y, text in items:
            pg.insert_text((x, y), text, fontname="japan", fontsize=fontsize)
    doc.save(str(path))
    doc.close()
    return path


def spaced(s: str) -> str:
    """スキャナのOCRがよく入れる字間の空白（「令 和 ６ 年」）。"""
    return " ".join(s)


def image_only_pdf(path: Path):
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
    doc.save(str(path))
    doc.close()
    return path
