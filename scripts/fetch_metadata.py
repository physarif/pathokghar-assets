"""
Fetch a single book's metadata (+ author name) from the Pathok Ghar
Firebase Realtime Database, using the plain REST API (no Admin SDK needed
if the DB rules allow public read on /books and /authors).

Usage:
    python scripts/fetch_metadata.py <book-slug>

Writes: work/metadata.json
"""
import json
import os
import sys
import urllib.request

DB_URL = "https://pathokghar-default-rtdb.asia-southeast1.firebasedatabase.app"


def fetch_json(path):
    auth = os.environ.get("FIREBASE_AUTH", "").strip()
    url = f"{DB_URL}/{path}.json"
    if auth:
        url += f"?auth={auth}"
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_metadata.py <book-slug>", file=sys.stderr)
        sys.exit(1)

    slug = sys.argv[1].strip()

    books = fetch_json("books") or {}
    book = None
    for uid, b in books.items():
        if b.get("slug") == slug:
            book = dict(b)
            book["uid"] = uid
            break

    if not book:
        print(f"ERROR: no book found with slug '{slug}'", file=sys.stderr)
        sys.exit(1)

    author_name = ""
    if book.get("author"):
        author = fetch_json(f"authors/{book['author']}") or {}
        author_name = author.get("title", "")

    if not book.get("zip"):
        print("ERROR: this book has no 'zip' field in the database", file=sys.stderr)
        sys.exit(1)

    meta = {
        "slug": book.get("slug"),
        "title": book.get("title", ""),
        "author_name": author_name,
        "cover_url": book.get("img", ""),
        "zip_url": book.get("zip", ""),
        "desc": book.get("desc", ""),
    }

    os.makedirs("work", exist_ok=True)
    with open("work/metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
