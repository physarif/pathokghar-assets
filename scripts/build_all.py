"""
Shared book-conversion driver. Fetches all books from Firebase, skips any
whose output file already exists in the repo (output/<format>/<slug>.<ext>),
and builds the missing ones for the requested format.

Usage:
    python scripts/build_all.py --format epub
    python scripts/build_all.py --format pdf
    python scripts/build_all.py --format mobi

Design per format comes from styles/<format>.css, so tweaking the look of
one format never touches the others.
"""
import argparse
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import zipfile

DB_URL = "https://pathokghar-default-rtdb.asia-southeast1.firebasedatabase.app"
WORK = "work"

EXT = {"epub": "epub", "pdf": "pdf", "mobi": "mobi"}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_json(path):
    auth = os.environ.get("FIREBASE_AUTH", "").strip()
    url = f"{DB_URL}/{path}.json"
    if auth:
        url += "?" + urllib.parse.urlencode({"auth": auth})
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def download(url, dest):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        out.write(resp.read())


def numeric_key(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else float("inf")


def list_pending_books(fmt):
    print("Fetching all books...")
    books_raw = fetch_json("books") or {}
    print("Fetching all authors...")
    authors_raw = fetch_json("authors") or {}

    out_dir = os.path.join("output", fmt)
    ext = EXT[fmt]

    pending = []
    for uid, b in books_raw.items():
        slug = b.get("slug")
        if not slug:
            continue
        if not b.get("zip"):
            print(f"SKIP {slug}: no zip field in database")
            continue
        out_path = os.path.join(out_dir, f"{slug}.{ext}")
        if os.path.exists(out_path):
            continue  # already built, in repo
        author_name = ""
        author_uid = b.get("author")
        if author_uid and author_uid in authors_raw:
            author_name = authors_raw[author_uid].get("title", "")
        pending.append({
            "uid": uid,
            "slug": slug,
            "title": b.get("title", ""),
            "author_name": author_name,
            "cover_url": b.get("img", ""),
            "zip_url": b.get("zip", ""),
        })
    return pending


def extract_html_files(zip_path, extract_dir):
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)
    html_files = []
    for root, _, files in os.walk(extract_dir):
        for fn in files:
            if fn.lower().endswith((".html", ".htm")):
                html_files.append(os.path.join(root, fn))
    html_files.sort(key=numeric_key)
    return html_files


def build_epub(book, html_files, cover_path, css_path, out_path):
    cmd = [
        "pandoc", *html_files,
        "-o", out_path,
        "--toc",
        "--epub-chapter-level=2",
        f"--metadata=title:{book['title']}",
        f"--metadata=author:{book.get('author_name', '')}",
    ]
    if cover_path:
        cmd.append(f"--epub-cover-image={cover_path}")
    if css_path and os.path.exists(css_path):
        cmd.append(f"--css={css_path}")
    subprocess.run(cmd, check=True)


def build_pdf_or_mobi(epub_path, out_path, css_path, fmt):
    cmd = ["ebook-convert", epub_path, out_path]
    if css_path and os.path.exists(css_path):
        cmd.append(f"--extra-css={css_path}")
    if fmt == "pdf":
        cmd += [
            "--pdf-default-font-size", "14",
            "--pdf-serif-family", "Noto Serif Bengali",
            "--pdf-sans-family", "Noto Sans Bengali",
            "--pdf-mono-family", "Noto Sans Mono",
            "--paper-size", "a5",
        ]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", required=True, choices=["epub", "pdf", "mobi"])
    args = ap.parse_args()
    fmt = args.format

    os.makedirs(WORK, exist_ok=True)
    out_dir = os.path.join("output", fmt)
    os.makedirs(out_dir, exist_ok=True)
    css_path = os.path.join("styles", f"{fmt}.css")

    pending = list_pending_books(fmt)
    print(f"New books to convert for '{fmt}': {len(pending)}")
    for b in pending:
        print(" -", b["slug"])

    for b in pending:
        slug = b["slug"]
        work_dir = os.path.join(WORK, slug)
        extract_dir = os.path.join(work_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            zip_path = os.path.join(work_dir, "book.zip")
            print(f"[{slug}] downloading zip")
            download(b["zip_url"], zip_path)
            html_files = extract_html_files(zip_path, extract_dir)
            if not html_files:
                print(f"[{slug}] SKIP: no HTML files in zip")
                continue

            cover_path = None
            if b.get("cover_url"):
                cover_path = os.path.join(work_dir, "cover.jpg")
                try:
                    download(b["cover_url"], cover_path)
                except Exception as e:
                    print(f"[{slug}] cover download failed: {e}")
                    cover_path = None

            if fmt == "epub":
                out_path = os.path.join(out_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, css_path, out_path)
            else:
                # pdf / mobi both go through an intermediate epub build
                # (kept in work/, not committed) so chapter splitting stays
                # consistent across all three formats.
                tmp_epub = os.path.join(work_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, os.path.join("styles", "epub.css"), tmp_epub)
                out_path = os.path.join(out_dir, f"{slug}.{EXT[fmt]}")
                build_pdf_or_mobi(tmp_epub, out_path, css_path, fmt)

            print(f"[{slug}] DONE -> {out_path}")
        except subprocess.CalledProcessError as e:
            print(f"[{slug}] FAILED (conversion): {e}")
            continue
        except Exception as e:
            print(f"[{slug}] FAILED: {e}")
            continue


if __name__ == "__main__":
    main()
