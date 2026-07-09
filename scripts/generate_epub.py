"""
Builds EPUB books. Fetches all books from Firebase, skips any already
published (via --existing-file), and builds the missing ones.

Usage:
    python scripts/generate_epub.py
"""
import os

import common


def build_one(book, html_files, cover_path, work_dir, extract_dir, out_dir, css_path, ctx):
    out_path = os.path.join(out_dir, f"{book['slug']}.epub")
    common.build_epub(book, html_files, cover_path, css_path, out_path, extract_dir=extract_dir)
    return out_path


if __name__ == "__main__":
    common.run("epub", "epub", build_one)
