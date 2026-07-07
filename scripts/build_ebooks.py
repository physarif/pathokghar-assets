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

# PDF-only: pandoc auto-generates a title+author page from --metadata
# title/author. For PDF we replace that page with a centered banner image
# and a donation message pinned to the bottom (see customize_pdf_title_page).
PDF_DONATE_BANNER_URL = "https://pathokghar.pages.dev/assets/photos/og-banner.webp"

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


def download(url, dest, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
            out.write(resp.read())
            return resp.headers.get("Content-Type", "")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Download failed (timeout={timeout}s): {e}") from e


CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def sniff_image_ext(path):
    """Detect the real image format from its file signature (magic bytes),
    since servers often send a wrong or missing Content-Type header. This
    is far more reliable than trusting headers or the URL's extension."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:2] == b"BM":
        return ".bmp"
    return None


def guess_image_ext(path, url, content_type):
    sniffed = sniff_image_ext(path)
    if sniffed:
        return sniffed
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in CONTENT_TYPE_EXT:
            return CONTENT_TYPE_EXT[ct]
    url_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if url_ext in CONTENT_TYPE_EXT.values():
        return url_ext
    return ".jpg"  # last-resort fallback guess


def natural_sort_key(path, base_dir=None):
    """Sort key that understands numbers anywhere in the path, not just a
    leading digit in the filename. Handles patterns like chapter1.html,
    chapter2.html, chapter10.html (numeric order, not string order) as
    well as numbered folders like 01/index.html, 02/index.html. Falls back
    to case-insensitive alphabetical order for the non-numeric parts so
    files without any numbering still get a stable, sensible order instead
    of the arbitrary order os.walk() happens to return."""
    rel = os.path.relpath(path, base_dir) if base_dir else path
    rel = rel.replace(os.sep, "/")
    parts = re.split(r"(\d+)", rel)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


HEADING_TAG_RE = re.compile(r"(</?)h[2-6](\b[^>]*)?(>)", re.IGNORECASE)


def flatten_headings_to_h1(html_path):
    """Rewrite every h2-h6 tag in the file to h1, preserving any attributes
    on the tag. h1 tags are left untouched."""
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    new_content = HEADING_TAG_RE.sub(lambda m: f"{m.group(1)}h1{m.group(2) or ''}{m.group(3)}", content)
    if new_content != content:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(new_content)


BENGALI_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def bengali_number(n):
    return str(n).translate(BENGALI_DIGITS)


H1_BLOCK_RE = re.compile(r"<h1([^>]*)>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _unescape_html_entities(text):
    """Decode HTML entities (&nbsp; -> space, &lt; -> <, etc.) and normalize
    whitespace."""
    import html as html_lib
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")  # &nbsp; and non-breaking space
    text = text.replace("\u200b", "")  # zero-width space
    return text


def first_h1_text(content):
    """Return the stripped, tag-free text of the first <h1> in the content,
    or None if there's no <h1> or it has no real text (e.g. it only wraps
    an image, or is empty/whitespace)."""
    m = H1_BLOCK_RE.search(content)
    if not m:
        return None
    text = TAG_RE.sub("", m.group(2))  # strip HTML tags
    text = _unescape_html_entities(text)  # decode entities + normalize spaces
    text = text.strip()
    return text or None


def ensure_toc_title(html_path, fallback_title):
    """Guarantee the epub reader's TOC/drawer always has a real, visible
    entry for this file. Two problems show up in messy book HTML:

    1. The real chapter title isn't in a heading at all (styled as a bold
       <p>, or baked into an image), so there's no <h1> for pandoc to pull
       TOC text from -> the chapter is missing from the drawer.
    2. A leftover/broken <h1> exists (empty, whitespace-only, or only
       wraps an image with no text) -> since every <h1> is a pandoc split
       point, this creates an extra phantom "chapter" with a blank or
       broken-looking TOC entry right next to the real one.

    Fix: demote every h1 that has no real text to a plain <div> (keeping
    its original content, e.g. a decorative image, intact) so it can't
    create a phantom TOC entry. Then, if no h1 with real text remains in
    the file at all, inject one fallback <h1> at the top of the body so
    the chapter always gets exactly one clean TOC entry."""
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    matches = list(H1_BLOCK_RE.finditer(content))
    good_exists = False
    pieces = []
    last_end = 0
    for m in matches:
        pieces.append(content[last_end:m.start()])
        text = TAG_RE.sub("", m.group(2))
        text = _unescape_html_entities(text)
        text = text.strip()
        if text:
            good_exists = True
            pieces.append(m.group(0))  # keep a genuine heading as-is
        else:
            attrs = m.group(1) or ""
            pieces.append(f"<div{attrs}>{m.group(2)}</div>")  # demote, keep content
        last_end = m.end()
    pieces.append(content[last_end:])
    new_content = "".join(pieces)

    if not good_exists:
        injected = f"<h1>{fallback_title}</h1>"
        body_match = re.search(r"(<body\b[^>]*>)", new_content, re.IGNORECASE)
        if body_match:
            insert_at = body_match.end()
            new_content = new_content[:insert_at] + injected + new_content[insert_at:]
        else:
            new_content = injected + new_content

    if new_content != content:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(new_content)


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
                full_path = os.path.join(root, fn)
                html_files.append(full_path)
    
    if not html_files:
        print(f"  WARNING: No HTML files found in {zip_path}")
        print(f"  Zip contents (first 20 entries):")
        with zipfile.ZipFile(zip_path) as z:
            for name in list(z.namelist())[:20]:
                print(f"    - {name}")
        return html_files
    
    html_files.sort(key=lambda p: natural_sort_key(p, extract_dir))
    for i, hf in enumerate(html_files, start=1):
        flatten_headings_to_h1(hf)
        ensure_toc_title(hf, f"অধ্যায় {bengali_number(i)}")
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
        "--split-level=1",
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
    if cover_path:
        remove_cover_page_from_reading_order(out_path)


def remove_cover_page_from_reading_order(epub_path):
    """Pandoc puts the cover image's own page (cover.xhtml) as the first
    page you flip through. We want the cover to still show as the book's
    thumbnail/cover in the reader's library (that comes from the manifest's
    cover-image entry, which we leave untouched) but NOT be a page in the
    reading flow, so the title page becomes page 1. This drops just the
    <itemref idref="cover_xhtml".../> line from the spine."""
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="epub_fix_")
    try:
        with zipfile.ZipFile(epub_path) as z:
            names = z.namelist()
            z.extractall(tmp_dir)

        opf_path = None
        for root, _, files in os.walk(tmp_dir):
            for fn in files:
                if fn.endswith(".opf"):
                    opf_path = os.path.join(root, fn)
        if not opf_path:
            return  # unexpected structure, leave the epub as-is

        with open(opf_path, "r", encoding="utf-8") as f:
            opf = f.read()
        new_opf = re.sub(r'\s*<itemref[^>]*idref="cover_xhtml"[^>]*/>', "", opf)
        if new_opf == opf:
            return  # nothing to change
        with open(opf_path, "w", encoding="utf-8") as f:
            f.write(new_opf)

        rebuilt_path = epub_path + ".rebuilt"
        with zipfile.ZipFile(rebuilt_path, "w") as zout:
            # mimetype must be the first entry and stored uncompressed
            zout.write(os.path.join(tmp_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
            for name in names:
                if name == "mimetype":
                    continue
                zout.write(os.path.join(tmp_dir, name), name, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(rebuilt_path, epub_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def customize_pdf_title_page(epub_path, banner_image_path):
    """PDF-only. Pandoc auto-generates a title+author page
    (EPUB/text/title_page.xhtml, <section class="titlepage">) from the
    --metadata title/author flags in build_epub(). For PDF we don't want
    that page at all: we replace its contents with a centered banner image
    and a donation message pinned to the bottom of the page. epub/mobi are
    untouched since this is only ever called when fmt == 'pdf'."""
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="epub_titlepage_")
    try:
        with zipfile.ZipFile(epub_path) as z:
            names = z.namelist()
            z.extractall(tmp_dir)

        title_page_path = None
        for root, _, files in os.walk(tmp_dir):
            if "title_page.xhtml" in files:
                title_page_path = os.path.join(root, "title_page.xhtml")
                break
        if not title_page_path:
            print("  (no title_page.xhtml found, skipping PDF title-page customization)")
            return

        # Drop the banner image next to title_page.xhtml so a plain relative
        # <img src="..."> resolves correctly inside the epub package.
        text_dir = os.path.dirname(title_page_path)
        ext = sniff_image_ext(banner_image_path) or ".webp"
        image_filename = f"og-banner{ext}"
        shutil.copy(banner_image_path, os.path.join(text_dir, image_filename))
        # New file on disk, not part of the original zip's namelist(), so it
        # must be added to the rebuild list explicitly below.
        image_zip_name = os.path.relpath(os.path.join(text_dir, image_filename), tmp_dir)
        names.append(image_zip_name)

        with open(title_page_path, "r", encoding="utf-8") as f:
            xhtml = f.read()

        new_section = f"""<section epub:type="titlepage" class="titlepage donate-titlepage">
  <div class="donate-banner">
    <img src="{image_filename}" alt="পাঠক ঘর" />
  </div>
  <div class="donate-footer">
    <p class="donate-line1"><b>পাঠক</b> <span class="donate-gray">ঘর</span> বিজ্ঞাপনমুক্ত রাখতে <b class="donate-red">ডোনেট</b> করুন।</p>
    <p class="donate-line2"><span class="donate-number">01318069471</span> – (<span class="donate-bkash">bKash</span>, <span class="donate-nagad">Nagad</span> – Personal)</p>
  </div>
</section>"""

        xhtml, count = re.subn(
            r'<section epub:type="titlepage" class="titlepage">.*?</section>',
            new_section,
            xhtml,
            flags=re.DOTALL,
        )
        if count == 0:
            print("  (title_page.xhtml didn't match expected structure, skipping)")
            return

        with open(title_page_path, "w", encoding="utf-8") as f:
            f.write(xhtml)

        # Register the new image in the .opf manifest so it's a proper,
        # valid part of the epub package (not just a stray file next to
        # title_page.xhtml).
        opf_path = None
        for root, _, files in os.walk(tmp_dir):
            for fn in files:
                if fn.endswith(".opf"):
                    opf_path = os.path.join(root, fn)
        if opf_path:
            with open(opf_path, "r", encoding="utf-8") as f:
                opf = f.read()
            opf_dir = os.path.dirname(opf_path)
            image_href = os.path.relpath(os.path.join(text_dir, image_filename), opf_dir).replace(os.sep, "/")
            media_type = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
            }.get(ext, "image/webp")
            manifest_item = (
                f'<item id="donate_banner_image" href="{image_href}" media-type="{media_type}" />\n  </manifest>'
            )
            opf = opf.replace("</manifest>", manifest_item, 1)
            with open(opf_path, "w", encoding="utf-8") as f:
                f.write(opf)

        rebuilt_path = epub_path + ".rebuilt"
        with zipfile.ZipFile(rebuilt_path, "w") as zout:
            zout.write(os.path.join(tmp_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
            for name in names:
                if name == "mimetype":
                    continue
                zout.write(os.path.join(tmp_dir, name), name, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(rebuilt_path, epub_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pdf_common_args():
    """Calibre flags shared by every PDF conversion pass (font/margins).
    Page-size flags (--paper-size / --custom-size) are passed separately
    per pass since front-matter and chapters now use different sizes."""
    return [
        "--pdf-default-font-size", "22",  # larger for mobile reading
        "--pdf-serif-family", "Noto Serif Bengali",
        "--pdf-sans-family", "Noto Sans Bengali",
        "--pdf-mono-family", "Noto Sans Mono",
        "--pdf-standard-font", "serif",
        "--pdf-mono-font-size", "16",
        # --pdf-hyphenate বাদ দেওয়া হলো: Calibre-এর বাংলার জন্য কোনো
        # hyphenation dictionary নেই, তাই এটা বাংলা টেক্সটে কার্যত কিছুই
        # করত না। right-edge ক্লিপিং সমস্যার আসল সমাধান হয়েছে pdf.css-এ
        # (body { overflow-wrap: anywhere; word-break: break-word; })
        # PDF-এর জন্য আসল margin flag এগুলো
        # যার default 72pt — তাই এই flag গুলো দিয়েই override করতে হয়
        # চারপাশে বাফার: বাম-ডান একটু বেশি (4mm ~11.34pt), উপর-নিচ 2mm (~5.67pt)
        "--pdf-page-margin-left", "11.34",
        "--pdf-page-margin-right", "11.34",
        "--pdf-page-margin-top", "5.67",
        "--pdf-page-margin-bottom", "5.67",
        # উপরের pdf-specific margin flag থাকা সত্ত্বেও Calibre-এর generic
        # --margin-* (ডিফল্ট 5pt প্রতিটা) নীরবে যুক্ত হয়ে যাচ্ছিল —
        # left-এ যোগ হয়ে বামপাশ বেশি চওড়া করে দিচ্ছিল, right থেকে বিয়োগ
        # হয়ে ডানপাশ সরু করে দিচ্ছিল (পরীক্ষায় নিশ্চিত হওয়া গেছে: left
        # ~15.9pt vs right ~6.3pt, যদিও দুটোই 11.34pt হওয়ার কথা)।
        # এখানে শূন্য করে দেওয়ায় দুই পাশ সত্যিকারের সমান হয়।
        "--margin-left", "0",
        "--margin-right", "0",
        "--margin-top", "0",
        "--margin-bottom", "0",
    ]


# Whole book (cover + donate/title page + chapters) is rendered at a
# single consistent size: A4, with a larger font (see styles/pdf.css) for
# comfortable reading — a named/standard calibre paper size, unlike
# --custom-size which proved unreliable.
PDF_SIZE_ARGS = ["--paper-size", "a4"]


def _run_ebook_convert(src_epub, dst_path, extra_args):
    cmd = ["ebook-convert", src_epub, dst_path, *extra_args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


def _filter_epub_spine(epub_path, keep_only=None, exclude=None, strip_cover=False):
    """Rewrite an epub's <spine> in place to keep only (or exclude) the
    given itemref idrefs. Manifest entries are left untouched (unused
    entries are harmless). If strip_cover is True, also removes the
    <meta name="cover" .../> entry so calibre doesn't auto-insert the
    cover image as page 1 of this particular conversion pass."""
    import shutil
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="epub_spine_")
    try:
        with zipfile.ZipFile(epub_path) as z:
            names = z.namelist()
            z.extractall(tmp_dir)

        opf_path = None
        for root, _, files in os.walk(tmp_dir):
            for fn in files:
                if fn.endswith(".opf"):
                    opf_path = os.path.join(root, fn)
        if not opf_path:
            return

        with open(opf_path, "r", encoding="utf-8") as f:
            opf = f.read()

        def filter_itemref(m):
            idref_match = re.search(r'idref="([^"]+)"', m.group(0))
            idref = idref_match.group(1) if idref_match else None
            if keep_only is not None:
                return m.group(0) if idref in keep_only else ""
            if exclude is not None:
                return "" if idref in exclude else m.group(0)
            return m.group(0)

        opf = re.sub(r"<itemref[^>]*/>", filter_itemref, opf)

        if strip_cover:
            opf = re.sub(r'<meta\s+name="cover"[^/]*/>', "", opf)

        with open(opf_path, "w", encoding="utf-8") as f:
            f.write(opf)

        rebuilt_path = epub_path + ".rebuilt"
        with zipfile.ZipFile(rebuilt_path, "w") as zout:
            zout.write(os.path.join(tmp_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
            for name in names:
                if name == "mimetype":
                    continue
                zout.write(os.path.join(tmp_dir, name), name, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(rebuilt_path, epub_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _merge_pdfs(pdf_paths, out_path):
    """Concatenate PDFs page-by-page. Each source page keeps its own
    mediabox, so the result can freely mix page sizes (e.g. A6 front
    matter followed by 19.5:9 chapter pages)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for path in pdf_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
    with open(out_path, "wb") as f:
        writer.write(f)


def build_pdf_single_size(intermediate_epub_path, out_path, work_dir):
    """PDF-only. Converts the whole book (cover + donate/title page +
    chapters) in a single calibre pass, all rendered at A6 size."""
    _run_ebook_convert(intermediate_epub_path, out_path, [*_pdf_common_args(), *PDF_SIZE_ARGS])


def build_pdf_or_mobi(epub_path, out_path, fmt, work_dir=None):
    if fmt == "pdf":
        build_pdf_single_size(epub_path, out_path, work_dir)
        return
    # mobi: single pass, no page-size split (mobi reflows, doesn't need it)
    cmd = ["ebook-convert", epub_path, out_path]
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

    # PDF-only: download the donate banner once up front (same image for
    # every book), so each book's build just copies it into its own epub.
    pdf_banner_path = None
    if fmt == "pdf":
        pdf_banner_path = os.path.join(WORK, "donate_banner_download")
        try:
            download(PDF_DONATE_BANNER_URL, pdf_banner_path)
            print(f"Downloaded PDF donate banner from {PDF_DONATE_BANNER_URL}")
        except Exception as e:
            print(f"WARNING: failed to download PDF donate banner ({e}); "
                  f"title+author page will NOT be replaced for this run.")
            pdf_banner_path = None

    pending = list_pending_books(fmt, existing_names)

    # ⚠️ TESTING MODE: শুধু প্রথম ৫টা বই build হবে, বাকিগুলো এখন skip।
    # টেস্টিং শেষ হলে নিচের লাইন দুটো (TEST_LIMIT ও pending স্লাইসিং) মুছে ফেলুন।
    TEST_LIMIT = 5
    if len(pending) > TEST_LIMIT:
        print(f"[TEST MODE] Limiting to first {TEST_LIMIT} of {len(pending)} pending books")
        pending = pending[:TEST_LIMIT]

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
            print(f"[{slug}] 1/5: downloading zip...", flush=True)
            download(b["zip_url"], zip_path)
            
            print(f"[{slug}] 2/5: extracting html...", flush=True)
            html_files = extract_html_files(zip_path, extract_dir)
            if not html_files:
                print(f"[{slug}] SKIP: no HTML files in zip")
                skipped.append((slug, "no HTML files in zip"))
                continue

            cover_path = None
            if b.get("cover_url"):
                print(f"[{slug}] 3/5: downloading cover...", flush=True)
                tmp_cover = os.path.join(work_dir, "cover_download")
                try:
                    content_type = download(b["cover_url"], tmp_cover)
                    ext = guess_image_ext(tmp_cover, b["cover_url"], content_type)
                    cover_path = os.path.join(work_dir, f"cover{ext}")
                    os.replace(tmp_cover, cover_path)
                except Exception as e:
                    print(f"[{slug}] cover download failed: {e}")
                    cover_path = None
            else:
                print(f"[{slug}] 3/5: (no cover)", flush=True)

            if fmt == "epub":
                print(f"[{slug}] 4/5: building epub...", flush=True)
                out_path = os.path.join(out_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, css_path, out_path, extract_dir=extract_dir)
                print(f"[{slug}] 5/5: done", flush=True)
            else:
                # pdf / mobi both go through an intermediate epub build
                # (kept in work/, not committed) so chapter splitting stays
                # consistent across all three formats. The intermediate epub
                # is built directly with the target format's own css (e.g.
                # pdf.css), baked in by pandoc, so there's no second,
                # possibly-conflicting --extra-css layer applied later.
                print(f"[{slug}] 4/5: building temp epub...", flush=True)
                tmp_epub = os.path.join(work_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, css_path, tmp_epub, extract_dir=extract_dir)

                if fmt == "pdf" and pdf_banner_path:
                    customize_pdf_title_page(tmp_epub, pdf_banner_path)

                print(f"[{slug}] 5/5: building {fmt.upper()}...", flush=True)
                out_path = os.path.join(out_dir, f"{slug}.{EXT[fmt]}")
                build_pdf_or_mobi(tmp_epub, out_path, fmt, work_dir=work_dir)

            print(f"[{slug}] ✓ DONE -> {out_path}", flush=True)
            succeeded.append(slug)
        except subprocess.CalledProcessError as e:
            print(f"[{slug}] ✗ FAILED (conversion), command: {' '.join(map(str, e.cmd))}")
            if e.stderr:
                print(f"[{slug}] --- stderr ---\n{e.stderr}")
            if e.output:
                print(f"[{slug}] --- stdout ---\n{e.output}")
            failed.append((slug, "conversion error (see stderr above)"))
            continue
        except Exception as e:
            print(f"[{slug}] ✗ FAILED: {type(e).__name__}: {e}")
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
