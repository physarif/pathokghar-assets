"""
Builds PDF books. Fetches all books from Firebase, skips any already
published (via --existing-file), and builds the missing ones.

Usage:
    python scripts/generate_pdf.py

PDF is built directly from the extracted chapter HTML (no pandoc/epub/
calibre step): styles/pdf.css controls layout, and scripts/render_pdf.js
(Paged.js via pagedjs-cli) does the actual page layout + PDF export.

Two page sizes are used — cover+donate at A6, chapters at a phone-screen
ratio — and since Chromium's print-to-PDF can only output ONE page size
per PDF (even though Paged.js itself lays each page out at its own size
correctly), the two parts are rendered as separate PDFs and then joined at
the file level with merge_pdfs, where mixed page sizes are completely
normal.
"""
import os
import re
import subprocess
import urllib.parse

import common

# pandoc auto-generates a title+author page; PDF instead gets a centered
# banner image + donation message on its own donate/title page.
PDF_DONATE_BANNER_URL = "https://pathokghar.pages.dev/assets/photos/og-banner.webp"

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
    if m is None:
        print(f"    DEBUG: no <body> tag matched in {html_path} "
              f"(len={len(content)}); using full file content as-is")
    id_count = len(ID_ATTR_RE.findall(inner))
    inner = ID_ATTR_RE.sub("", inner)
    stripped_len = len(re.sub(r"<[^>]+>", "", inner).strip())
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


def _css_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def pdf_chapters_page_css(title):
    """Chapter content: phone-screen-ratio page, plus a running footer
    showing the book title (left, static), the page number in Bengali
    digits (center), and the current chapter's name (right, updates per
    page via the h1 { string-set: chapter-title content(); } rule in
    pdf.css)."""
    safe_title = _css_escape(title)
    return (
        "@page {"
        "  size: 110mm 210mm;"
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
        '    content: "পাঠক ঘর";'
        '    font-family: "Noto Sans Bengali", "Kalpurush", sans-serif;'
        "    font-size: 9px;"
        "    font-weight: 700;"
        "    color: #888;"
        "  }"
        "}"
    )


def build_pdf_front_html(book, cover_path, banner_image_path, css_path, out_html_path):
    """Assembles just the cover + donate/title page as their own
    standalone HTML document, at A6 size. Rendered to its own PDF (see
    render_pdf) and later merged with the chapters PDF (see merge_pdfs) —
    Chromium's print-to-PDF only supports ONE page size per PDF export,
    even though Paged.js itself lays every page out correctly at its own
    size; the two different sizes we want (A6 cover/donate vs. phone-ratio
    chapters) only survive if they're generated as two separate PDF files
    and joined together afterwards at the file level, where mixed page
    sizes are completely normal."""
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
    """Assembles every chapter (concatenated) as its own standalone HTML
    document, at the phone-screen-ratio size. See build_pdf_front_html for
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


def render_pdf(html_path, out_path):
    """Hands the assembled HTML off to scripts/render_pdf.js, which uses
    pagedjs-cli (Paged.js + Puppeteer) to lay it out page by page and print
    the result to a PDF."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_pdf.js")
    cmd = ["node", script_path, html_path, out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(f"  --- render_pdf.js stdout ---\n{result.stdout}")
    if result.stderr:
        print(f"  --- render_pdf.js stderr ---\n{result.stderr}")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout, stderr=result.stderr,
        )


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


def setup(work_root):
    """Download the donate banner once up front (same image for every
    book), embedded directly into each book's assembled donate page HTML."""
    tmp_banner = os.path.join(work_root, "donate_banner_download")
    try:
        common.download(PDF_DONATE_BANNER_URL, tmp_banner)
        ext = common.sniff_image_ext(tmp_banner) or ".webp"
        pdf_banner_path = os.path.join(work_root, f"donate_banner{ext}")
        os.replace(tmp_banner, pdf_banner_path)
        print(f"Downloaded PDF donate banner from {PDF_DONATE_BANNER_URL}")
    except Exception as e:
        print(f"WARNING: failed to download PDF donate banner ({e}); "
              f"donate page will be skipped for this run.")
        pdf_banner_path = None
    return {"pdf_banner_path": pdf_banner_path}


def build_one(book, html_files, cover_path, work_dir, extract_dir, out_dir, css_path, ctx):
    slug = book["slug"]
    pdf_banner_path = ctx.get("pdf_banner_path")

    front_html = os.path.join(work_dir, f"{slug}.front.pdf.html")
    chapters_html = os.path.join(work_dir, f"{slug}.chapters.pdf.html")
    build_pdf_chapters_html(book, html_files, css_path, chapters_html)

    front_pdf = None
    if cover_path or pdf_banner_path:
        build_pdf_front_html(book, cover_path, pdf_banner_path, css_path, front_html)
        front_pdf = os.path.join(work_dir, f"{slug}.front.pdf")
        render_pdf(front_html, front_pdf)

    chapters_pdf = os.path.join(work_dir, f"{slug}.chapters.pdf")
    render_pdf(chapters_html, chapters_pdf)

    out_path = os.path.join(out_dir, f"{slug}.pdf")
    merge_pdfs([front_pdf, chapters_pdf], out_path)
    return out_path


if __name__ == "__main__":
    common.run("pdf", "pdf", build_one, setup=setup)
