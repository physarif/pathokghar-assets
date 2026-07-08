#!/usr/bin/env node
/**
 * Renders a self-contained HTML file (with its @page / Paged.js CSS already
 * linked, e.g. styles/pdf.css) to a PDF using Paged.js.
 *
 * Engine note: this used to hand-roll "Playwright + inject the Paged.js
 * polyfill script manually", but that combination has a reproducible bug —
 * Paged.js correctly computes every page internally (its own `flow.total`
 * is correct), yet only page 1 ever gets attached to the DOM before
 * page.pdf() runs, so every book came out as a 1-page PDF (just whatever
 * static content sat before the first real page break). This is a known
 * class of issue with driving Paged.js's polyfill through a bare CDP
 * session (see pagedjs/pagedjs#183 and gitlab pagedmedia/pagedjs#198 —
 * "Puppeteer/headless Chrome output contains only one page").
 *
 * pagedjs-cli (https://github.com/pagedjs/pagedjs-cli) is the Paged.js
 * project's own official headless renderer. It drives the exact same
 * Paged.js core, but through Puppeteer with the correct low-level
 * page-by-page event wiring (exposeFunction("onPage", ...) per rendered
 * page, resolving only once Paged.js's own "rendered" event fires) that
 * our hand-rolled version was missing. Verified locally: the same combined
 * HTML that produced a 1-page PDF through raw Playwright produced the
 * correct 10-page PDF through pagedjs-cli. So: still Paged.js, still a
 * Chromium-family headless browser, just via the tool that has already
 * solved this specific integration problem.
 *
 * Usage:
 *   node scripts/render_pdf.js <input.html> <output.pdf>
 */
import path from "path";
import fs from "fs";
import Printer from "pagedjs-cli";

async function main() {
  const [, , inputHtml, outputPdf] = process.argv;
  if (!inputHtml || !outputPdf) {
    console.error("Usage: node render_pdf.js <input.html> <output.pdf>");
    process.exit(1);
  }
  if (!fs.existsSync(inputHtml)) {
    console.error(`Input HTML not found: ${inputHtml}`);
    process.exit(1);
  }

  const printer = new Printer({
    allowLocal: true, // needed for our file:// image/CSS references
    // --no-sandbox: GitHub Actions (and some other CI/containers) run the
    // job as root, where Chromium's sandbox refuses to start otherwise.
    browserArgs: ["--no-sandbox", "--disable-web-security", "--disable-dev-shm-usage"],
    timeout: 10 * 60 * 1000, // large books can take a while to paginate
    enableWarnings: true,
  });

  printer.on("page", (p) => {
    if (p && typeof p.position === "number") {
      console.log(`[render_pdf] rendered page ${p.position + 1}`);
    }
  });

  try {
    const pdf = await printer.pdf("file://" + path.resolve(inputHtml), {
      // Bonus: this also gives every h1 a bookmark/outline entry in the
      // PDF viewer's sidebar (chapter navigation), essentially for free.
      outlineTags: ["h1"],
    });
    fs.writeFileSync(outputPdf, pdf);
    console.log(`[render_pdf] wrote ${outputPdf} (${printer.pages.length} pages)`);
  } finally {
    await printer.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
