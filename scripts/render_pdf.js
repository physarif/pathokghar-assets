#!/usr/bin/env node
/**
 * Renders a self-contained HTML file (with its @page / Paged.js CSS already
 * linked, e.g. styles/pdf.css) to a PDF using the Paged.js polyfill running
 * inside a real Chromium instance (via Playwright), then prints that laid
 * out result with Chromium's own PDF engine.
 *
 * This replaces the old Calibre-based PDF pipeline: page size, margins,
 * running headers/footers, Bengali page-number counters and heading page
 * breaks are all expressed as plain CSS (see styles/pdf.css) instead of
 * Calibre command-line flags, so there is exactly one source of truth for
 * how a PDF page looks.
 *
 * Usage:
 *   node scripts/render_pdf.js <input.html> <output.pdf>
 */
const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright");

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

  // pagedjs's package.json restricts subpath resolution via its "exports"
  // map, so require.resolve() can't reach dist/paged.polyfill.js directly.
  // node_modules lives at the project root (where package.json/npm install
  // was run), one level up from this scripts/ folder.
  const pagedPolyfillPath = path.join(
    __dirname, "..", "node_modules", "pagedjs", "dist", "paged.polyfill.js"
  );

  const browser = await chromium.launch({
    // file:// image/CSS loading needs this relaxed in headless Chromium
    args: ["--allow-file-access-from-files", "--disable-web-security"],
  });
  const page = await browser.newPage();

  page.on("console", (msg) => console.log("[page]", msg.text()));
  page.on("pageerror", (err) => console.error("[pageerror]", err));
  page.on("requestfailed", (request) => {
    const failure = request.failure();
    console.log(
      `[requestfailed] ${request.url()} -- ${failure ? failure.errorText : "unknown error"}`
    );
  });

  // Tell Paged.js not to auto-run on script load; we start it ourselves
  // after wiring up a reliable "done" signal via the `after` hook (the
  // officially documented way to know rendering has finished — see
  // https://pagedjs.org/documentation/2-getting-started-with-paged.js/).
  await page.addInitScript(() => {
    window.__pagedjsDone = false;
    window.PagedConfig = {
      auto: false,
      after: () => {
        window.__pagedjsDone = true;
      },
    };
  });

  await page.goto("file://" + path.resolve(inputHtml), { waitUntil: "load" });

  await page.addScriptTag({ path: pagedPolyfillPath });

  await page.evaluate(() => {
    window.PagedPolyfill.preview();
  });

  // Large books can take a while to paginate; give it up to 10 minutes.
  await page.waitForFunction(() => window.__pagedjsDone === true, {
    timeout: 10 * 60 * 1000,
  });

  // DEBUG: find out whether Paged.js itself only produced a couple of
  // page boxes (meaning pagination stopped early inside Paged.js) or
  // whether it produced all of them and something in the print step is
  // the one dropping pages.
  const pageInfo = await page.evaluate(() => {
    const pages = document.querySelectorAll(".pagedjs_page");
    return {
      pagedjsPageCount: pages.length,
      bodyScrollHeight: document.body.scrollHeight,
      pagesContainerHeight: document.querySelector(".pagedjs_pages")
        ? document.querySelector(".pagedjs_pages").scrollHeight
        : null,
    };
  });
  console.log(
    `[render_pdf] DEBUG: Paged.js produced ${pageInfo.pagedjsPageCount} ` +
    `page div(s); body.scrollHeight=${pageInfo.bodyScrollHeight}px; ` +
    `.pagedjs_pages height=${pageInfo.pagesContainerHeight}px`
  );

  // Playwright's page.pdf() forces "print" media by default. Paged.js lays
  // out each page as its own absolutely-positioned div under "screen"
  // media; if Chromium then applies its own native print pagination on top
  // of that (because we're in "print" media), the two conflicting layouts
  // collapse into a single page. Explicitly staying in "screen" media for
  // the pdf() call is the documented fix for this well-known Paged.js +
  // headless-Chromium issue.
  await page.emulateMedia({ media: "screen" });

  await page.pdf({
    path: outputPdf,
    printBackground: true,
    preferCSSPageSize: true,
  });

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
