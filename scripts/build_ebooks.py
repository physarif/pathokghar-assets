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

    # DEBUG: show exactly what was found and how big each chapter file is,
    # so we can tell from the Actions log whether the zip's HTML actually
    # made it into the pipeline before any further processing touches it.
    print(f"  DEBUG: found {len(html_files)} html file(s) in {zip_path}:")
    for hf in html_files:
        try:
            size = os.path.getsize(hf)
        except OSError:
            size = -1
        print(f"    - {os.path.relpath(hf, extract_dir)} ({size} bytes)")

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


BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
IMG_SRC_RE = re.compile(r'(<img[^>]+?src=)(["\'])(.*?)\2', re.IGNORECASE)
ID_ATTR_RE = re.compile(r'\s+id=(["\'])[^"\']*\1', re.IGNORECASE)


def _extract_body_inner(html_path):
    """Return a chapter HTML file's <body> inner content, with every
    relative <img src="...">/<img src='...'> rewritten to an absolute
    file:// path (each chapter file may live in its own extracted
    subfolder, so once several chapters are concatenated into one combined
    document a single shared base path no longer works for all of them).
    Handles both single- and double-quoted src attributes, since some
    epub-generation tools (Calibre, Sigil, etc.) emit single-quoted
    attributes on auto-generated files like cover.xhtml.

    Also strips every id="..." attribute. Each chapter file was originally
    its own standalone epub document, often with auto-generated ids
    (calibre_pb_1, pgepubid00000, etc.) that are only unique *within that
    one file*. Once many chapter files are concatenated into a single
    combined document, identical ids reused across chapters collide into
    duplicate ids — pagedjs 0.4.x has a known bug where a document with
    duplicate ids/text nodes silently mis-paginates or stops early. PDF
    output has no need for in-page anchors, so it's safe to drop them."""
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    m = BODY_RE.search(content)
    inner = m.group(1) if m else content
    # DEBUG: flag files where the <body> regex didn't match (falling back to
    # the whole file) and files that are suspiciously short/empty after
    # extraction, since either case would silently produce a blank chapter.
    if m is None:
        print(f"    DEBUG: no <body> tag matched in {html_path} "
              f"(len={len(content)}); using full file content as-is")
    id_count = len(ID_ATTR_RE.findall(inner))
    inner = ID_ATTR_RE.sub("", inner)
    stripped_len = len(TAG_RE.sub("", inner).strip())
    print(f"    DEBUG: {os.path.basename(html_path)} -> body inner "
          f"{len(inner)} chars, {stripped_len} chars of visible text, "
          f"stripped {id_count} id attr(s)")
    # IMPORTANT: base_dir must be absolute. html_path itself is often a
    # relative path (e.g. "work/<slug>/extracted/OEBPS/Text/x.xhtml"), so
    # os.path.dirname() on it is still relative — prefixing "file://" onto
    # a relative path produces an invalid/misresolved URL that the browser
    # silently fails to load, which was breaking Paged.js pagination.
    base_dir = os.path.abspath(os.path.dirname(html_path))

    def fix_img(m2):
        prefix, quote, src = m2.group(1), m2.group(2), m2.group(3)
        if src.startswith(("http://", "https://", "data:", "file://")):
            return m2.group(0)
        abs_path = os.path.normpath(os.path.join(base_dir, urllib.parse.unquote(src)))
        if not os.path.exists(abs_path):
            print(f"    DEBUG: WARNING image not found on disk: {abs_path} "
                  f"(original src={src!r} in {os.path.basename(html_path)})")
        return f"{prefix}{quote}file://{abs_path}{quote}"

    return IMG_SRC_RE.sub(fix_img, inner)


def _pdf_html_head(title, css_path, page_css):
    css_abs = os.path.abspath(css_path)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="bn">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f'<link rel="stylesheet" href="file://{css_abs}">\n'
        # Injected here (not in pdf.css) because Chromium's print-to-PDF
        # only honors one @page rule per render — see the note at the top
        # of styles/pdf.css for why.
        f"<style>{page_css}</style>\n"
        "</head>\n"
        "<body>\n"
    )


# Cover/donate page: A6 dimensions written out explicitly in mm — Chromium's
# print-to-PDF only recognizes a small hardcoded list of named page-size
# keywords (confirmed: "A4" works, "A6" silently falls back to US Letter),
# so named sizes other than that short list must be spelled out as
# width/height instead of relying on the keyword.
PDF_FRONT_PAGE_CSS = "@page { size: 105mm 148mm; margin: 0; }"

# Chapter content: phone-screen-ratio page, plus a running footer showing
# the book title (left, static), the page number in Bengali digits
# (center), and the current chapter's name (right, updates per page via
# the h1 { string-set: chapter-title content(); } rule in pdf.css).
def _css_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def pdf_chapters_page_css(title):
    safe_title = _css_escape(title)
    return (
        "@page {"
        "  size: 100mm 217mm;"
        "  margin: 10mm 8mm 18mm 8mm;"
        "  @bottom-left {"
        f'    content: "{safe_title}";'
        '    font-family: "Noto Sans Bengali", "Kalpurush", sans-serif;'
        "    font-size: 9px;"
        "    color: #888;"
        "  }"
        "  @bottom-center {"
        "    content: counter(page, bengali);"
        '    font-family: "Noto Sans Bengali", "Kalpurush", sans-serif;'
        "    font-size: 10px;"
        "    color: #000;"
        "  }"
        "  @bottom-right {"
        "    content: string(chapter-title);"
        '    font-family: "Noto Sans Bengali", "Kalpurush", sans-serif;'
        "    font-size: 9px;"
        "    color: #888;"
        "  }"
        "}"
    )


def build_pdf_front_html(book, cover_path, banner_image_path, css_path, out_html_path):
    """PDF-only. Assembles just the cover + donate/title page as their own
    standalone HTML document, at A6 size (styles/pdf.css's named
    "cover-page" @page rule). Rendered to its own PDF (see render_pdf) and
    later merged with the chapters PDF (see merge_pdfs) — Chromium's
    print-to-PDF only supports ONE page size per PDF export, even though
    Paged.js itself lays every page out correctly at its own size; the two
    different sizes we want (A6 cover/donate vs. phone-ratio chapters)
    only survive if they're generated as two separate PDF files and joined
    together afterwards at the file level, where mixed page sizes are
    completely normal."""
    title = book.get("title", "")
    parts = [_pdf_html_head(title, css_path, PDF_FRONT_PAGE_CSS)]

    if cover_path:
        cover_abs = os.path.abspath(cover_path)
        parts.append(
            f'<section class="cover-page"><img src="file://{cover_abs}" alt="cover"></section>\n'
        )

    if banner_image_path:
        banner_abs = os.path.abspath(banner_image_path)
        parts.append(
            '<section class="donate-titlepage">\n'
            '  <div class="donate-banner">\n'
            f'    <img src="file://{banner_abs}" alt="পাঠক ঘর" />\n'
            "  </div>\n"
            '  <div class="donate-footer">\n'
            '    <p class="donate-line1"><b>পাঠক</b> <span class="donate-gray">ঘর</span> '
            'বিজ্ঞাপনমুক্ত রাখতে <b class="donate-red">ডোনেট</b> করুন।</p>\n'
            '    <p class="donate-line2"><span class="donate-number">01318069471</span> '
            '\u2013 (<span class="donate-bkash">bKash</span>, '
            '<span class="donate-nagad">Nagad</span> \u2013 Personal)</p>\n'
            "  </div>\n"
            "</section>\n"
        )

    parts.append("</body></html>")
    final_html = "".join(parts)
    with open(out_html_path, "w", encoding="utf-8") as f:
        f.write(final_html)

    print(f"  DEBUG: assembled front (cover/donate) html -> {len(final_html)} chars, "
          f"has cover: {'cover-page' in final_html}, has donate: {'donate-titlepage' in final_html}")


def build_pdf_chapters_html(book, html_files, css_path, out_html_path):
    """PDF-only. Assembles every chapter (concatenated) as its own
    standalone HTML document, at the phone-screen-ratio size (styles/
    pdf.css's default/unnamed @page rule). See build_pdf_front_html for
    why this is a separate document from the cover/donate page."""
    title = book.get("title", "")
    parts = [_pdf_html_head(title, css_path, pdf_chapters_page_css(title))]

    for hf in html_files:
        parts.append(f'<section class="chapter">{_extract_body_inner(hf)}</section>\n')

    parts.append("</body></html>")
    final_html = "".join(parts)
    with open(out_html_path, "w", encoding="utf-8") as f:
        f.write(final_html)

    chapter_marker = 'class="chapter"'
    print(f"  DEBUG: assembled chapters html -> {len(final_html)} chars, "
          f"chapter sections: {final_html.count(chapter_marker)}")
    remaining_ids = re.findall(r'\sid=(["\'])([^"\']*)\1', final_html)
    if remaining_ids:
        ids_only = [rid for _, rid in remaining_ids]
        dupes = {i for i in ids_only if ids_only.count(i) > 1}
        print(f"  DEBUG: {len(ids_only)} id attr(s) remain in chapters html; "
              f"duplicates: {dupes if dupes else 'none'}")


def merge_pdfs(pdf_paths, out_path):
    """Join several PDFs (each internally uniform-sized, e.g. one A6 file
    and one phone-ratio file) into a single output PDF. Unlike Chromium's
    print-to-PDF step, the PDF file format itself has no problem with
    different pages having different sizes, so a plain page-level merge is
    all that's needed here."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for p in pdf_paths:
        if p and os.path.exists(p):
            writer.append(p)
    with open(out_path, "wb") as f:
        writer.write(f)

def render_pdf(html_path, out_path):
    """PDF-only. Hands the assembled HTML off to scripts/render_pdf.js,
    which uses pagedjs-cli (Paged.js + Puppeteer) to lay it out page by
    page and print the result to a PDF."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_pdf.js")
    cmd = ["node", script_path, html_path, out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Always surface the node script's own console output (our [page],
    # [pageerror] and DEBUG lines), not just when it fails — otherwise
    # everything it logs is silently swallowed on a "successful" run.
    if result.stdout:
        print(f"  --- render_pdf.js stdout ---\n{result.stdout}")
    if result.stderr:
        print(f"  --- render_pdf.js stderr ---\n{result.stderr}")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


def _run_ebook_convert(src_epub, dst_path, extra_args):
    cmd = ["ebook-convert", src_epub, dst_path, *extra_args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


def build_mobi(epub_path, out_path):
    """MOBI-only: single calibre pass, no page-size concerns (mobi reflows
    like any other e-reader format)."""
    _run_ebook_convert(epub_path, out_path, [])

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
    # every book), embedded directly into the assembled donate page HTML.
    pdf_banner_path = None
    if fmt == "pdf":
        tmp_banner = os.path.join(WORK, "donate_banner_download")
        try:
            download(PDF_DONATE_BANNER_URL, tmp_banner)
            ext = sniff_image_ext(tmp_banner) or ".webp"
            pdf_banner_path = os.path.join(WORK, f"donate_banner{ext}")
            os.replace(tmp_banner, pdf_banner_path)
            print(f"Downloaded PDF donate banner from {PDF_DONATE_BANNER_URL}")
        except Exception as e:
            print(f"WARNING: failed to download PDF donate banner ({e}); "
                  f"donate page will be skipped for this run.")
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
            elif fmt == "pdf":
                # PDF is built directly from the extracted chapter HTML
                # (no intermediate epub / pandoc / calibre step). Two page
                # sizes are used — cover+donate at A6, chapters at a
                # phone-screen ratio — and since Chromium's print-to-PDF
                # can only output ONE page size per PDF (even though
                # Paged.js itself lays each page out at its own size
                # correctly), the two parts are rendered as separate PDFs
                # and then joined at the file level with merge_pdfs, where
                # mixed page sizes are completely normal. Page size,
                # margins and headings-start-new-page are all plain CSS in
                # styles/pdf.css, laid out by Paged.js via pagedjs-cli.
                print(f"[{slug}] 4/6: assembling HTML (front + chapters)...", flush=True)
                front_html = os.path.join(work_dir, f"{slug}.front.pdf.html")
                chapters_html = os.path.join(work_dir, f"{slug}.chapters.pdf.html")
                build_pdf_chapters_html(b, html_files, css_path, chapters_html)

                front_pdf = None
                if cover_path or pdf_banner_path:
                    build_pdf_front_html(b, cover_path, pdf_banner_path, css_path, front_html)
                    print(f"[{slug}] 5/6: rendering front (A6) PDF...", flush=True)
                    front_pdf = os.path.join(work_dir, f"{slug}.front.pdf")
                    render_pdf(front_html, front_pdf)
                else:
                    print(f"[{slug}] 5/6: (no cover/banner, skipping front PDF)", flush=True)

                print(f"[{slug}] 6/6: rendering chapters PDF + merging...", flush=True)
                chapters_pdf = os.path.join(work_dir, f"{slug}.chapters.pdf")
                render_pdf(chapters_html, chapters_pdf)

                out_path = os.path.join(out_dir, f"{slug}.pdf")
                merge_pdfs([front_pdf, chapters_pdf], out_path)
            else:
                # mobi goes through an intermediate epub build (kept in
                # work/, not committed) the same way it always has, since
                # mobi readers reflow text and don't need page-size/CSS
                # Paged Media handling the way PDF does.
                print(f"[{slug}] 4/5: building temp epub...", flush=True)
                tmp_epub = os.path.join(work_dir, f"{slug}.epub")
                build_epub(b, html_files, cover_path, css_path, tmp_epub, extract_dir=extract_dir)

                print(f"[{slug}] 5/5: building mobi...", flush=True)
                out_path = os.path.join(out_dir, f"{slug}.mobi")
                build_mobi(tmp_epub, out_path)

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
