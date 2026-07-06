# pathokghar-assets

Pathok Ghar এর বই এর HTML zip থেকে EPUB + PDF জেনারেট করার জন্য আলাদা repo।
Firebase Realtime Database (`pathokghar-default-rtdb`) থেকে metadata (title,
author, cover, zip URL) fetch করে, চ্যাপ্টার order zip এর ভেতরের HTML
ফাইলের numeric prefix (`1.html`, `2.html`, ...) অনুযায়ী ঠিক হয়, আর অধ্যায়
বিভাজন হয় pandoc এর `--epub-chapter-level` দিয়ে (H1/H2 ভিত্তিক)।

## ব্যবহার

1. GitHub repo এর **Actions** ট্যাবে যাও → **Build Book (EPUB + PDF)** workflow সিলেক্ট করো
2. **Run workflow** চাপো, `slug` ইনপুটে বইয়ের slug দাও (যেমন `octopus-er-chokh`)
3. Run শেষ হলে repo এর **Releases** ট্যাবে ঐ slug নামে একটা release পাবে, তাতে `.epub` আর `.pdf` attach থাকবে
4. Download link হবে:
   ```
   https://github.com/physarif/pathokghar-assets/releases/download/<slug>/<slug>.epub
   https://github.com/physarif/pathokghar-assets/releases/download/<slug>/<slug>.pdf
   ```
   এই লিংক Pathok Ghar এর `download.html` এ সরাসরি ব্যবহার করা যাবে।

## Firebase auth (যদি লাগে)

`fetch_metadata.py` সরাসরি RTDB REST API (`/books.json`, `/authors/<uid>.json`)
পাবলিকভাবে read করে। যদি DB rules এ read-protected থাকে, তাহলে:

1. Firebase Console → Realtime Database → Rules চেক করো (`.read` public আছে কিনা)
2. না থাকলে, repo এর **Settings → Secrets → Actions** এ `FIREBASE_DB_AUTH` নামে
   secret বানাও — এখানে একটা legacy database secret অথবা কোনো valid ID token বসাও
3. Workflow স্বয়ংক্রিয়ভাবে সেটা query param হিসেবে ব্যবহার করবে

## Zip ফাইলের প্রত্যাশিত structure

```
book.zip
├── 1.html   ← প্রথম অধ্যায়, ভেতরে <h1>/<h2> দিয়ে অধ্যায়ের নাম
├── 2.html
├── 3.html
└── ...
```

## Local এ টেস্ট করতে চাইলে

```bash
sudo apt-get install -y pandoc calibre
python scripts/fetch_metadata.py octopus-er-chokh
python scripts/build_book.py
# output/octopus-er-chokh.epub, output/octopus-er-chokh.pdf
```
