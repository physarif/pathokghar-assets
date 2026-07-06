"""
Fetch a single book's metadata (+ author name) from the Pathok Ghar
Firebase Realtime Database, using the plain REST API (no Admin SDK needed
if the DB rules allow public read on /books and /authors).

Uses a server-side filtered query (orderBy="slug"&equalTo=...) so it only
downloads the ONE matching book, not the entire /books node.
NOTE: this requires a ".indexOn": ["slug"] rule on /books in the Realtime
Database rules, otherwise Firebase still works but warns/slows down for
large datasets. See README for the rule to add.

Usage:
    python scripts/fetch_metadata.py <book-slug>

Writes: work/metadata.json
"""
import json
import os
import sys
import urllib.parse
import urllib.request

DB_URL = "https://pathokghar-default-rtdb.asia-southeast1.firebasedatabase.app"


def fetch_json(path, extra_params=None):
    auth = os.environ.get("FIREBASE_AUTH", "").strip()
    params = dict(extra_params or {})
    if auth:
        params["auth"] = auth
    url = f"{DB_URL}/{path}.json"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as r:
        return json.load(r)


def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_metadata.py <book-slug>", file=sys.stderr)
        sys.exit(1)

    slug = sys.argv[1].strip()

    # Server-side filtered query: only the matching book comes back over
    # the wire, not the whole /books node.
    result = fetch_json(
        "books",
        {
            "orderBy": json.dumps("slug"),
            "equalTo": json.dumps(slug),
        },
    ) or {}

    if not result:
        print(f"ERROR: no book found with slug '{slug}'", file=sys.stderr)
        sys.exit(1)

    uid, book = next(iter(result.items()))
    book = dict(book)
    book["uid"] = uid

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
