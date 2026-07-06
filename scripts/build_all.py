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
        return resp.headers.get("Content-Type", "")


CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def guess_image_ext(url, content_type):
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in CONTENT_TYPE_EXT:
            return CONTENT_TYPE_EXT[ct]
    url_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if url_ext in CONTENT_TYPE_EXT.values():
        return url_ext
    return ".jpg"  # fallback guess


def numeric_key(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else float("inf")


def list_pending_books(fmt, existing_names=None):
    print("Fetching all books...")
    books_raw = fetch_json("books") or {}
    print("Fetching all authors...")
    authors_raw = fetch_json("authors") or {}

    ext = EXT[fmt]
    existing_names = existing_names or set()

    pending = []
    for uid, b in books_raw.items():
        slug = b.get("slug")
        if not slug:
            continue
        if not b.get("zip"):
            print(f"SKIP {slug}: no zip field in database")
            continue
        filename = f"{slug}.{ext}"
        if filename in existing_names:
            continue  # already built and published in the release
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
            if fn.lower().endswith((".html", ".htm", ".xhtml")):
                html_files.append(os.path.join(root, fn))
    html_files.sort(key=numeric_key)
    return html_files


def build_epub(book, html_files, cover_path, css_path, out_path, extract_dir=None):
    resource_dirs = []
    if extract_dir:
        resource_dirs.append(extract_dir)
    # Each HTML file may reference images relative to its own folder,
    # so include every distinct parent directory as well.
    for hf in html_files:
        parent = os.path.dirname(hf)
        if parent not in resource_dirs:
            resource_dirs.append(parent)

    cmd = [
        "pandoc", *html_files,
        "-o", out_path,
        "--toc",
        "--epub-chapter-level=2",
        f"--metadata=title:{book['title']}",
        f"--metadata=author:{book.get('author_name', '')}",
    ]
    if resource_dirs:
        cmd.append(f"--resource-path={os.pathsep.join(resource_dirs)}")
    if cover_path:
        cmd.append(f"--epub-cover-image={cover_path}")
    if css_path and os.path.exists(css_path):
        cmd.append(f"--css={css_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", required=True, choices=["epub", "pdf", "mobi"])
    ap.add_argument(
        "--existing-file",
        default=None,
        help=(
            "Path to a text file listing filenames already published "
            "(e.g. as GitHub release assets), one per line. Books whose "
            "output filename appears here are skipped."
        ),
    )
    args = ap.parse_args()
    fmt = args.format

    existing_names = set()
    if args.existing_file and os.path.exists(args.existing_file):
        with open(args.existing_file) as f:
            existing_names = {line.strip() for line in f if line.strip()}
        print(f"Loaded {len(existing_names)} already-published filenames from {args.existing_file}")

    os.makedirs(WORK, exist_ok=True)
    out_dir = os.path.join("output", fmt)
    os.makedirs(out_dir, exist_ok=True)
    css_path = os.path.join("styles", f"{fmt}.css")

    pending = list_pending_books(fmt, existing_names)
    print(f"New books to convert for '{fmt}': {len(pending)}")
    for b in pending:
        print(" -", b["slug"])

    succeeded = []
    failed = []
    skipped = []

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
                skipped.append((slug, "no HTML files in zip"))
                continue

            cover_path = None
            if b.get("cover_url"):
                tmp_cover = os.path.join(work_dir, "cover_download")
                try:
                    content_type = download(b["cover_url"], tmp_cover)
                    ext = guess_image_ext(b["cover_url"], content_type)
                    cover_path = os.path.join(work_dir, f"cover{ext}")
                    os.replace(tmp_cover, cover_path)
                except Exception as e:
                    print(f"[{slug}] cover download failed: {e}")
                    cover_path = None

            if fmt == "epub":
                out_path = os.path.join(out_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, css_path, out_path, extract_dir=extract_dir)
            else:
                # pdf / mobi both go through an intermediate epub build
                # (kept in work/, not committed) so chapter splitting stays
                # consistent across all three formats.
                tmp_epub = os.path.join(work_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, os.path.join("styles", "epub.css"), tmp_epub, extract_dir=extract_dir)
                out_path = os.path.join(out_dir, f"{slug}.{EXT[fmt]}")
                build_pdf_or_mobi(tmp_epub, out_path, css_path, fmt)

            print(f"[{slug}] DONE -> {out_path}")
            succeeded.append(slug)
        except subprocess.CalledProcessError as e:
            print(f"[{slug}] FAILED (conversion), command: {' '.join(map(str, e.cmd))}")
            if e.stderr:
                print(f"[{slug}] --- stderr ---\n{e.stderr}")
            if e.output:
                print(f"[{slug}] --- stdout ---\n{e.output}")
            failed.append((slug, "conversion error (see stderr above)"))
            continue
        except Exception as e:
            print(f"[{slug}] FAILED: {type(e).__name__}: {e}")
            failed.append((slug, f"{type(e).__name__}: {e}"))
            continue

    print("\n===== BUILD SUMMARY =====")
    print(f"Succeeded: {len(succeeded)}")
    print(f"Failed:    {len(failed)}")
    print(f"Skipped:   {len(skipped)}")
    if failed:
        print("\nFailed books:")
        for slug, reason in failed:
            print(f"  - {slug}: {reason}")
    if skipped:
        print("\nSkipped books:")
        for slug, reason in skipped:
            print(f"  - {slug}: {reason}")


if __name__ == "__main__":
    main()
