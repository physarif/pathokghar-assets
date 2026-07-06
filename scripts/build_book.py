"""
Download the book's HTML zip + cover image, then build EPUB and PDF.

Chapter order comes from the numeric filename prefix (1.html, 2.html, ...).
Chapter headings inside each file (h1/h2) are used by pandoc's own
--epub-chapter-level splitting, so no manual HTML parsing is required.

Usage:
    python scripts/build_book.py

Reads: work/metadata.json (produced by fetch_metadata.py)
Writes: output/<slug>.epub, output/<slug>.pdf
"""
import json
import os
import re
import subprocess
import urllib.request
import zipfile

WORK = "work"
EXTRACT = os.path.join(WORK, "extracted")
OUTPUT = "output"


def download(url, dest):
    urllib.request.urlretrieve(url, dest)


def numeric_key(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else float("inf")


def main():
    with open(os.path.join(WORK, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)

    os.makedirs(EXTRACT, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)

    zip_path = os.path.join(WORK, "book.zip")
    print("Downloading zip:", meta["zip_url"])
    download(meta["zip_url"], zip_path)

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(EXTRACT)

    html_files = []
    for root, _, files in os.walk(EXTRACT):
        for fn in files:
            if fn.lower().endswith((".html", ".htm")):
                html_files.append(os.path.join(root, fn))
    html_files.sort(key=numeric_key)

    if not html_files:
        raise SystemExit("No HTML files found inside the zip")

    print(f"Found {len(html_files)} chapter files, order:")
    for h in html_files:
        print(" -", os.path.basename(h))

    cover_path = None
    if meta.get("cover_url"):
        cover_path = os.path.join(WORK, "cover.jpg")
        try:
            download(meta["cover_url"], cover_path)
        except Exception as e:
            print("Cover download failed (continuing without cover):", e)
            cover_path = None

    slug = meta["slug"]
    epub_path = os.path.join(OUTPUT, f"{slug}.epub")
    pdf_path = os.path.join(OUTPUT, f"{slug}.pdf")

    pandoc_cmd = [
        "pandoc", *html_files,
        "-o", epub_path,
        "--toc",
        "--epub-chapter-level=2",
        f"--metadata=title:{meta['title']}",
        f"--metadata=author:{meta.get('author_name', '')}",
    ]
    if cover_path:
        pandoc_cmd.append(f"--epub-cover-image={cover_path}")

    print("Running:", " ".join(pandoc_cmd))
    subprocess.run(pandoc_cmd, check=True)

    calibre_cmd = ["ebook-convert", epub_path, pdf_path]
    print("Running:", " ".join(calibre_cmd))
    subprocess.run(calibre_cmd, check=True)

    print("Done ->", epub_path, pdf_path)


if __name__ == "__main__":
    main()
