"""
channels.txt içindeki kanalları kontrol edip turkiye.m3u üretir.

Her kanal için sırayla:
  1) Verdiğin linkler (birden fazla olabilir) sırayla test edilir, ilk çalışan kullanılır. Çalışıyorsa aynen kullanılır (daha yüksek kalite varsa yükseltilir).
  2) Çalışmıyorsa, verdiysen resmi canlı yayın sayfasından taze link çekilir.
  3) O da olmazsa eski link listede bırakılır. Kanal hiçbir zaman silinmez.

Kullanım:
  python official.py             bir kez çalıştır
  python official.py --browser   dinamik sayfalar için Playwright kullan
  python official.py --loop --serve   kendi bilgisayarında/sunucunda sürekli çalıştır ve sun
"""
import argparse
import concurrent.futures as cf
import functools
import html
import http.server
import os
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
OUTPUT = "turkiye.m3u"
CHANNELS_FILE = "channels.txt"
REFRESH_SECONDS = 24 * 3600
IPTV_ORG = "https://iptv-org.github.io/iptv/countries/tr.m3u"

M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.m3u8[^\s\"'<>\\]*", re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
URL_HEIGHT_RE = re.compile(r"_(\d{3,4})p\.m3u8", re.I)


# ---------- yardımcılar ----------
def norm(name):
    s = name.lower().replace("i̇", "i")
    s = s.translate(str.maketrans("çğıöşüâî", "cgiosuai"))
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(hd|fhd|uhd|sd|4k|hevc|h265|h264|\d{3,4}p)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r" tv$", "", s)
    return s.replace(" ", "")


def clean(text):
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/")
    return html.unescape(text)


def fetch(url, referer=None, timeout=12):
    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"}
    if referer:
        headers["Referer"] = referer
    return requests.get(url, headers=headers, timeout=timeout)


def is_direct(src):
    return src.lower().startswith("http") and ".m3u8" in src.lower()


def height_from_url(url):
    m = URL_HEIGHT_RE.search(url)
    return int(m.group(1)) if m else 0


def load_channels(path=CHANNELS_FILE):
    channels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = [x.strip() for x in line.split("|")]
            if len(p) in (3, 4) and all(p):
                channels.append({"cat": p[0], "name": p[1], "src": p[2], "page": p[3] if len(p) == 4 else ""})
            else:
                print("Atlandı (biçim hatalı):", line[:80])
    return channels


# ---------- yayın bulma ----------
def find_in_page(url, depth=1, seen=None):
    seen = seen if seen is not None else set()
    if url in seen:
        return []
    seen.add(url)
    try:
        r = fetch(url)
        if r.status_code != 200:
            return []
        text = clean(r.text)
    except Exception:
        return []
    found = [m.rstrip("\\,;)") for m in M3U8_RE.findall(text)]
    if not found and depth > 0:
        for src in IFRAME_RE.findall(text):
            found += find_in_page(urljoin(url, src), depth - 1, seen)
    return list(dict.fromkeys(found))


def find_with_browser(url, wait=10):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (playwright kurulu değil)")
        return []
    hits = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, locale="tr-TR")
        page.on("request", lambda req: hits.append(req.url) if ".m3u8" in req.url.lower() else None)
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(wait * 1000)
        except Exception:
            pass
        browser.close()
    return list(dict.fromkeys(hits))


def best_variant(body, base_url):
    """En yüksek çözünürlüklü alt yayını bulur; token/sorgu parametresini korur."""
    best = None
    base_query = urlparse(base_url).query
    lines = [l.strip() for l in body.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        key = (int(res.group(2)) if res else 0, int(bw.group(1)) if bw else 0)
        for nxt in lines[i + 1:]:
            if nxt and not nxt.startswith("#"):
                full = urljoin(base_url, nxt)
                if "?" not in full and base_query:
                    full += "?" + base_query
                cand = (key[0], key[1], full)
                if best is None or cand[:2] > best[:2]:
                    best = cand
                break
    return best


def validate(url, referer=None, pin=False):
    """Yayın açılıyor mu? pin=True ise en yüksek kaliteli alt yayına sabitler."""
    try:
        r = fetch(url, referer=referer, timeout=10)
        if r.status_code != 200 or not r.text.lstrip().startswith("#EXTM3U"):
            return None
        if pin:
            best = best_variant(r.text, r.url)
            if best:
                return {"url": best[2], "height": best[0], "bw": best[1]}
        return {"url": url, "height": height_from_url(url), "bw": 0}
    except Exception:
        return None


def upgrade_quality(res):
    """Link adında _720p gibi bir çözünürlük varsa 1440p/1080p karşılığını dener."""
    m = URL_HEIGHT_RE.search(res["url"])
    if not m:
        return res
    cur = int(m.group(1))
    for h in (1440, 1080):
        if h > cur:
            new = res["url"].replace(m.group(0), f"_{h}p.m3u8", 1)
            r2 = validate(new, res.get("referer"))
            if r2:
                r2["referer"] = res.get("referer")
                return r2
    return res


# ---------- iptv-org (isteğe bağlı) ----------
def iptv_org_index():
    index, info, ref = {}, None, None
    text = requests.get(IPTV_ORG, headers={"User-Agent": UA}, timeout=30).text
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            info, ref = line, None
        elif line.startswith("#EXTVLCOPT:http-referrer="):
            ref = line.split("=", 1)[1]
        elif line and not line.startswith("#") and info:
            index.setdefault(norm(info.rsplit(",", 1)[-1].strip()), []).append((line, ref))
            info, ref = None, None
    return index


# ---------- önceki liste ----------
def load_previous():
    prev = {}
    if not os.path.exists(OUTPUT):
        return prev
    name, ref = None, None
    with open(OUTPUT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#EXTINF"):
                name, ref = line.rsplit(",", 1)[-1].strip(), None
            elif line.startswith("#EXTVLCOPT:http-referrer="):
                ref = line.split("=", 1)[1]
            elif line and not line.startswith("#") and name:
                prev[name] = {"url": line, "referer": ref, "height": height_from_url(line)}
                name = None
    return prev


# ---------- ana akış ----------
def sources(ch):
    """3. sütun boşlukla ayrılmış birden fazla link/sayfa içerebilir (öncelik sırasıyla)."""
    return [x for x in ch["src"].split() if x]


def resolve(ch, use_browser, index):
    srcs, page = sources(ch), ch["page"]
    direct = [x for x in srcs if is_direct(x)]
    others = [x for x in srcs if not is_direct(x) and x.lower() != "iptv-org"]
    wants_org = "iptv-org" in [x.lower() for x in srcs] or page.lower() == "iptv-org"

    # 1) Verdiğin linkler sırayla test edilir; ilk çalışan kullanılır
    for src in direct:
        res = validate(src)
        if res:
            res["referer"] = None
            return ch, upgrade_quality(res), "mevcut"

    # 2) Resmi sayfadan taze link
    page_url = page if page and page.lower() != "iptv-org" else (others[0] if others else "")
    cands = []
    if page_url:
        urls = find_in_page(page_url)
        if not urls and use_browser:
            urls = find_with_browser(page_url)
        cands += [(u, page_url) for u in urls]

    # 3) iptv-org
    if wants_org:
        cands += index.get(norm(ch["name"]), [])

    best = None
    for url, ref in cands:
        res = validate(url, ref, pin=True)
        if res and (best is None or (res["height"], res["bw"]) > (best["height"], best["bw"])):
            res["referer"] = ref
            best = res
    return ch, best, "yenilendi"


def build(use_browser=False):
    channels = load_channels()
    wants_org = any("iptv-org" in c["src"].lower().split() or c["page"].lower() == "iptv-org" for c in channels)
    index = iptv_org_index() if wants_org else {}
    previous = load_previous()

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        outcomes = list(ex.map(lambda c: resolve(c, use_browser, index), channels))

    tmp = OUTPUT + ".tmp"
    n_ok = n_new = n_old = n_missing = 0
    log = []
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch, res, how in outcomes:
            name = ch["name"]
            if res:
                if how == "mevcut":
                    n_ok += 1
                    tag = "OK    "
                else:
                    n_new += 1
                    tag = "YENİ  "
            else:
                first = next((x for x in sources(ch) if is_direct(x)), None)
                res = previous.get(name) or (
                    {"url": first, "referer": None, "height": height_from_url(first)} if first else None
                )
                if not res:
                    n_missing += 1
                    log.append(f"  YOK    {name}")
                    continue
                n_old += 1
                tag = "ESKİ   "
            q = f' {res["height"]}p' if res.get("height") else ""
            log.append(f"  {tag}{name}{q}")
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="{ch["cat"]}",{name}\n')
            if res.get("referer"):
                f.write(f'#EXTVLCOPT:http-referrer={res["referer"]}\n')
            f.write(res["url"] + "\n")
    os.replace(tmp, OUTPUT)

    total = len(channels)
    print(f"[{time.strftime('%H:%M:%S')}] Toplam {total}: {n_ok} link çalışıyor, {n_new} yenilendi, "
          f"{n_old} eski link korundu (test edilemedi/çalışmıyor), {n_missing} bulunamadı")
    print("\n".join(log))
    if total and n_ok + n_new == 0:
        print("\nUYARI: Hiçbir yayın bu bilgisayardan/sunucudan doğrulanamadı. Linkler gerçekten ölmüş "
              "olabilir ya da yayınlar bu IP'yi (ör. GitHub'ın yurt dışı IP'si) engelliyor olabilir. "
              "Eski linkler listede korundu.")


def serve(port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=".")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Sunuluyor: http://<bu-bilgisayarın-ip'si>:{port}/{OUTPUT}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--browser", action="store_true")
    args = ap.parse_args()

    build(args.browser)
    if args.serve:
        serve(args.port)
    if args.loop:
        while True:
            time.sleep(REFRESH_SECONDS)
            try:
                build(args.browser)
            except Exception as e:
                print("Yenileme hatası:", e)
    elif args.serve:
        while True:
            time.sleep(3600)
