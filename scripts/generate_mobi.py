"""
Builds MOBI books. Fetches all books from Firebase, skips any already
published (via --existing-file), and builds the missing ones.

Usage:
    python scripts/generate_mobi.py

MOBI goes through an intermediate epub build (kept in work/, not
committed) since mobi readers reflow text and don't need the page-size/CSS
Paged Media handling PDF does — calibre's ebook-convert handles epub->mobi
well on its own.
"""
import os
import subprocess

import common


def _run_ebook_convert(src_epub, dst_path, extra_args):
    cmd = ["ebook-convert", src_epub, dst_path, *extra_args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


def build_mobi(epub_path, out_path):
    _run_ebook_convert(epub_path, out_path, [])


def build_one(book, html_files, cover_path, work_dir, extract_dir, out_dir, css_path, ctx):
    slug = book["slug"]
    tmp_epub = os.path.join(work_dir, f"{slug}.epub")
    common.build_epub(book, html_files, cover_path, css_path, tmp_epub, extract_dir=extract_dir)

    out_path = os.path.join(out_dir, f"{slug}.mobi")
    build_mobi(tmp_epub, out_path)
    return out_path


if __name__ == "__main__":
    common.run("mobi", "mobi", build_one)
