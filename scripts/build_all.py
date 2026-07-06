"""
Fetch ALL books from the Firebase Realtime Database, skip any whose slug
already has a GitHub Release (so re-runs only touch new books), and for
each remaining book: download the zip, build EPUB + PDF, and create a
GitHub Release tagged with that book's slug.

Requires: GITHUB_TOKEN env var (Actions provides this automatically) and
the `gh` CLI (preinstalled on GitHub-hosted ubuntu runners).
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import zipfile

DB_URL = "https://pathokghar-default-rtdb.asia-southeast1.firebasedatabase.app"
WORK = "work"
OUTPUT = "output"


def fetch_json(path):
    auth = os.environ.get("FIREBASE_AUTH", "").strip()
    url = f"{DB_URL}/{path}.json"
    if auth:
        url += "?" + urllib.parse.urlencode({"auth": auth})
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def existing_release_tags():
    out = subprocess.run(
        ["gh", "release", "list", "--limit", "1000", "--json", "tagName"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    return {item["tagName"] for item in data}


def download(url, dest):
    urllib.request.urlretrieve(url, dest)


def numeric_key(path):
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else float("inf")


def build_one(book, work_dir):
    slug = book["slug"]
    extract_dir = os.path.join(work_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)

    zip_path = os.path.join(work_dir, "book.zip")
    print(f"[{slug}] downloading zip: {book['zip_url']}")
    download(book["zip_url"], zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    html_files = []
    for root, _, files in os.walk(extract_dir):
        for fn in files:
            if fn.lower().endswith((".html", ".htm")):
                html_files.append(os.path.join(root, fn))
    html_files.sort(key=numeric_key)
    if not html_files:
        print(f"[{slug}] SKIP: no HTML files found in zip")
        return None

    cover_path = None
    if book.get("cover_url"):
        cover_path = os.path.join(work_dir, "cover.jpg")
        try:
            download(book["cover_url"], cover_path)
        except Exception as e:
            print(f"[{slug}] cover download failed: {e}")
            cover_path = None

    epub_path = os.path.join(OUTPUT, f"{slug}.epub")
    pdf_path = os.path.join(OUTPUT, f"{slug}.pdf")

    pandoc_cmd = [
        "pandoc", *html_files,
        "-o", epub_path,
        "--toc",
        "--epub-chapter-level=2",
        f"--metadata=title:{book['title']}",
        f"--metadata=author:{book.get('author_name', '')}",
    ]
    if cover_path:
        pandoc_cmd.append(f"--epub-cover-image={cover_path}")
    subprocess.run(pandoc_cmd, check=True)
    subprocess.run(["ebook-convert", epub_path, pdf_path], check=True)

    return epub_path, pdf_path


def create_release(slug, title, epub_path, pdf_path):
    subprocess.run(
        [
            "gh", "release", "create", slug,
            epub_path, pdf_path,
            "--title", title,
            "--notes", f"Auto-generated EPUB/PDF for {title}",
        ],
        check=True,
    )


def main():
    os.makedirs(WORK, exist_ok=True)

    print("Fetching all books...")
    books_raw = fetch_json("books") or {}
    print("Fetching all authors...")
    authors_raw = fetch_json("authors") or {}

    print("Checking existing releases...")
    done_tags = existing_release_tags()
    print(f"Already released: {len(done_tags)}")

    pending = []
    for uid, b in books_raw.items():
        slug = b.get("slug")
        if not slug or slug in done_tags:
            continue
        if not b.get("zip"):
            print(f"SKIP {slug}: no zip field in database")
            continue
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

    print(f"New books to convert: {len(pending)}")
    for b in pending:
        print(" -", b["slug"])

    for b in pending:
        slug = b["slug"]
        work_dir = os.path.join(WORK, slug)
        os.makedirs(work_dir, exist_ok=True)
        try:
            result = build_one(b, work_dir)
            if result is None:
                continue
            epub_path, pdf_path = result
            create_release(slug, b["title"] or slug, epub_path, pdf_path)
            print(f"[{slug}] DONE, release created")
        except subprocess.CalledProcessError as e:
            print(f"[{slug}] FAILED: {e}")
            continue


if __name__ == "__main__":
    main()
