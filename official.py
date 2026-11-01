"""
kanallar.m3u'daki linkleri kontrol eder.
  - Link ÇALIŞIYORSA hiç dokunmaz.
  - Link ÖLMÜŞSE ve sayfalar.txt'de o kanalın resmi sayfası varsa, sayfayı tarayıcıyla açıp
    yayının taze linkini yakalar ve kanallar.m3u'da SADECE o linki değiştirir.
  - Yeni link bulunamazsa eski link olduğu gibi kalır.
Logo, grup, kanal sırası ve YouTube gibi .m3u8 olmayan linkler hiç değişmez.

ayarlar.txt içinde canli_guncelleme = hayir ise sadece test eder, dosyaya yazmaz.
"""
import concurrent.futures as cf
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LIST_FILE = "kanallar.m3u"
PAGES_FILE = "sayfalar.txt"
SETTINGS_FILE = "ayarlar.txt"

BROWSER_BUDGET_SEC = 360      # bir çalıştırmada tarayıcıya ayrılan en fazla süre
MAX_PAGES_PER_RUN = 15        # bir çalıştırmada en fazla kaç sayfa açılır
MASS_FAILURE_RATIO = 0.5      # linklerin bundan fazlası aynı anda ölü görünürse hiçbir şeye dokunma
PAGE_WAIT_SEC = 10

URL_HEIGHT_RE = re.compile(r"_(\d{3,4})p\.m3u8", re.I)
RES_RE = re.compile(r"RESOLUTION=\d+x(\d+)")
BW_RE = re.compile(r"BANDWIDTH=(\d+)")
JUNK_RE = re.compile(r"chunklist|/media_|segment|\.ts(\?|$)", re.I)


# ---------- yardımcılar ----------
def norm(name):
    s = name.lower().replace("i̇", "i")
    s = s.translate(str.maketrans("çğıöşüâî", "cgiosuai"))
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r" tv$", "", s)
    return s.replace(" ", "")


def is_m3u8(url):
    return url.lower().startswith("http") and ".m3u8" in url.lower()


def domain(url):
    host = urlparse(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def read_settings():
    live = False
    if os.path.exists(SETTINGS_FILE):
        for line in open(SETTINGS_FILE, encoding="utf-8"):
            line = line.strip()
            if line.startswith("canli_guncelleme"):
                live = line.split("=", 1)[1].strip().lower() in ("evet", "yes", "true", "1")
    return live


def load_pages():
    pages = {}
    if os.path.exists(PAGES_FILE):
        for line in open(PAGES_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "|" in line:
                name, url = [x.strip() for x in line.split("|", 1)]
                pages[norm(name)] = url
    return pages


def parse_list():
    """Dosyayı satır satır okur; her kanalın link satırının numarasını tutar."""
    with open(LIST_FILE, encoding="utf-8-sig") as f:
        lines = f.read().replace("\r", "").split("\n")
    entries, cur = [], None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#EXTINF"):
            cur = {"name": s.rsplit(",", 1)[-1].strip()}
        elif s and not s.startswith("#") and cur is not None:
            cur["url"], cur["idx"] = s, i
            entries.append(cur)
            cur = None
    return lines, entries


# ---------- link testi ----------
def validate(url):
    """Çalışıyorsa {'url','height','bw'}, değilse None. Çözünürlük sadece sıralama içindir."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        if r.status_code != 200 or not r.text.lstrip().startswith("#EXTM3U"):
            return None
        heights = [int(x) for x in RES_RE.findall(r.text)]
        bws = [int(x) for x in BW_RE.findall(r.text)]
        m = URL_HEIGHT_RE.search(url)
        h = max(heights + ([int(m.group(1))] if m else [0]))
        return {"url": url, "height": h, "bw": max(bws or [0])}
    except Exception:
        return None


# ---------- tarayıcı ile taze link yakalama ----------
class Browser:
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required"]
        try:
            self.b = self.pw.chromium.launch(headless=True, args=args)
        except Exception:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=300, check=False)
            self.b = self.pw.chromium.launch(headless=True, args=args)
        return self

    def __exit__(self, *a):
        try:
            self.b.close()
            self.pw.stop()
        except Exception:
            pass

    def capture(self, url):
        ctx = self.b.new_context(user_agent=UA, locale="tr-TR", viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        hits = []
        page.on("request", lambda r: hits.append(r.url) if ".m3u8" in r.url.lower() else None)
        try:
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            try:
                page.evaluate("document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})})")
                page.mouse.click(640, 400)
            except Exception:
                pass
            page.wait_for_timeout(PAGE_WAIT_SEC * 1000)
        except Exception:
            pass
        ctx.close()
        return list(dict.fromkeys(hits))


def pick_fresh(hits, old_url):
    good = [h for h in hits if not JUNK_RE.search(h)]
    same = [h for h in good if domain(h) == domain(old_url)]   # reklam vb. yayınları ele
    best = None
    for h in (same or good):
        res = validate(h)
        if res and (best is None or (res["height"], res["bw"]) > (best["height"], best["bw"])):
            best = res
    return best


# ---------- ana akış ----------
def main():
    live = read_settings()
    pages = load_pages()
    lines, entries = parse_list()
    targets = [e for e in entries if is_m3u8(e["url"])]

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda e: validate(e["url"]), targets))
    dead = [e for e, r in zip(targets, results) if not r]
    ok = len(targets) - len(dead)

    print(f"[{time.strftime('%H:%M:%S')}] {len(entries)} kanal | {len(targets)} .m3u8 linki: {ok} çalışıyor, {len(dead)} ölü | "
          f"{len(entries) - len(targets)} link test edilemiyor (YouTube vb.) | mod: {'CANLI' if live else 'SADECE TEST'}")

    if len(targets) and len(dead) / len(targets) > MASS_FAILURE_RATIO:
        print(f"\nUYARI: Linklerin %{100 * len(dead) // len(targets)}'i aynı anda ölü görünüyor. Bu büyük ihtimalle linklerin "
              "değil, GitHub sunucusunun IP'sinin engellenmesinden kaynaklanıyor. Yanlışlıkla çalışan linkleri bozmamak "
              "için hiçbir şeye dokunulmadı.")
        for e in dead:
            print(f"  ÖLÜ?   {e['name']}")
        return

    changes, log = {}, []
    todo = {}
    for e in dead:
        page = pages.get(norm(e["name"]))
        if page:
            todo.setdefault(page, []).append(e)
        else:
            log.append(f"  ÖLÜ    {e['name']}  (sayfalar.txt'de adresi yok, dokunulmadı)")

    if todo:
        start = time.time()
        try:
            with Browser() as br:
                for n, (page, ents) in enumerate(todo.items()):
                    if n >= MAX_PAGES_PER_RUN or time.time() - start > BROWSER_BUDGET_SEC:
                        for e in ents:
                            log.append(f"  ÖLÜ    {e['name']}  (süre/sayfa sınırı, sonraki çalıştırmada denenecek)")
                        continue
                    best = pick_fresh(br.capture(page), ents[0]["url"])
                    for e in ents:
                        if best:
                            changes[e["idx"]] = best["url"]
                            q = f" {best['height']}p" if best["height"] else ""
                            log.append(f"  YENİ   {e['name']}{q}")
                        else:
                            log.append(f"  ÖLÜ    {e['name']}  (sayfadan çalışan link bulunamadı, eski link korundu)")
        except Exception as ex:
            log.append(f"  Tarayıcı çalıştırılamadı: {ex}")
            for ents in todo.values():
                for e in ents:
                    log.append(f"  ÖLÜ    {e['name']}  (tarayıcı yok, dokunulmadı)")

    print("\n".join(log) if log else "  Tüm linkler çalışıyor, değiştirilecek bir şey yok.")

    if changes and live:
        for idx, url in changes.items():
            lines[idx] = url
        tmp = LIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, LIST_FILE)
        print(f"\n{len(changes)} link güncellendi ve {LIST_FILE} kaydedildi.")
    elif changes:
        print(f"\nSADECE TEST modu: {len(changes)} link yenilenebilirdi ama {LIST_FILE} değiştirilmedi. "
              "Sonuçlar iyiyse ayarlar.txt'de canli_guncelleme = evet yap.")


if __name__ == "__main__":
    main()
