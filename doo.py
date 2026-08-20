"""
স্বয়ংক্রিয় বাংলা নিউজ বট — ৫ সোর্স (sitemap/RSS + og:image) → কার্ড → Facebook Page পোস্ট
ব্রাউজার অটোমেশন (কুকি GitHub Secrets-এ), DeepSeek/Blogger/API নেই।
"""
import os, re, json, time, random, hashlib, requests, jinja2, base64
import pytz
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BD_TZ = pytz.timezone("Asia/Dhaka")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Accept-Language": "bn,en;q=0.9,en;q=0.8"}

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "100089034123367")
CARD_CATEGORY = os.environ.get("CARD_CATEGORY", "সর্বশেষ")

MEDIA_DIR = "downloaded_media"
POSTED_CACHE = "posted_cache.txt"
CAPTCHA_LOCK_FILE = "captcha_lock.txt"
DAILY_LIMIT_FILE = "daily_post_limit.json"
TOPIC_MEMORY_FILE = "topic_memory.json"
MAX_DURATION = 6 * 3600
os.makedirs(MEDIA_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# ৫ বাংলা সোর্স
# ──────────────────────────────────────────────
SOURCES = [
    {"name": "ঢাকা পোস্ট", "base": "https://www.dhakapost.com", "kind": "sitemap",
     "maps": ["https://www.dhakapost.com/sitemaps/news-sitemap.xml"]},
    {"name": "আমার দেশ", "base": "https://www.dailyamardesh.com", "kind": "sitemap",
     "maps": ["https://www.dailyamardesh.com/news-sitemap.xml"]},
    {"name": "সমকাল", "base": "https://samakal.com", "kind": "samakal", "maps": []},
    {"name": "বণিকবার্তা", "base": "https://bonikbarta.com", "kind": "sitemap",
     "maps": ["https://bonikbarta.com/sitemap.xml"]},
    {"name": "ParsToday বাংলা", "base": "https://parstoday.ir", "kind": "rss",
     "maps": ["https://parstoday.ir/bn/rss"]},
]

# ──────────────────────────────────────────────
# SESSION (Cookie-Editor JSON অথবা Playwright storage_state — দুটোই চলবে)
# ──────────────────────────────────────────────
def _fix_samesite(v):
    if not v:
        return None
    return {"strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None"}.get(str(v).lower())

def normalize_cookies(raw):
    cookies = raw.get("cookies", []) if isinstance(raw, dict) else raw
    out = []
    for c in cookies:
        try:
            nc = {"name": c["name"], "value": c["value"],
                  "domain": c["domain"], "path": c.get("path", "/")}
            exp = c.get("expires", c.get("expirationDate"))
            if exp not in (None, -1) and not c.get("session", False):
                nc["expires"] = int(exp)
            nc["httpOnly"] = bool(c.get("httpOnly", False))
            nc["secure"] = bool(c.get("secure", False))
            ss = _fix_samesite(c.get("sameSite"))
            if ss:
                nc["sameSite"] = ss
            out.append(nc)
        except Exception:
            continue
    return out

def load_session():
    data = None
    s = os.environ.get("SESSION_JSON")
    if s:
        try:
            data = json.loads(s)
        except Exception as e:
            print(f"❌ SESSION_JSON parse error: {e}")
    if data is None and os.path.exists("session.json"):
        try:
            with open("session.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"❌ session.json error: {e}")
    if data is None:
        return None
    cookies = normalize_cookies(data)
    print(f"✅ Session normalized: {len(cookies)} cookies")
    return {"cookies": cookies, "origins": []}

def validate_session():
    sess = load_session()
    if not sess or not sess["cookies"]:
        print("❌ No session found (SESSION_JSON). Bot stopped.")
        return False
    return True

# ──────────────────────────────────────────────
# CHECKPOINT / SESSION-DEAD LOCK
# ──────────────────────────────────────────────
def is_captcha_locked():
    if not os.path.exists(CAPTCHA_LOCK_FILE):
        return False
    with open(CAPTCHA_LOCK_FILE, "r") as f:
        lock_time = float(f.read().strip())
    remaining = (12 * 3600) - (time.time() - lock_time)
    if remaining > 0:
        print(f"🔒 Lock active: {int(remaining // 3600)}h {int((remaining % 3600) // 60)}m remaining.")
        return True
    os.remove(CAPTCHA_LOCK_FILE)
    return False

def set_captcha_lock():
    with open(CAPTCHA_LOCK_FILE, "w") as f:
        f.write(str(time.time()))
    print("🔒 Lock set for 12h.")

def check_fb_health(page):
    url = page.url.lower()
    if "checkpoint" in url or "challenge" in url:
        page.screenshot(path=f"captcha_debug_{int(time.time())}.png")
        set_captcha_lock()
        return "locked"
    if "login.php" in url or "/login" in url:
        print("❌ Session dead — login page এ redirect.")
        page.screenshot(path=f"captcha_debug_{int(time.time())}.png")
        return "dead"
    try:
        txt = page.inner_text("body").lower()
        if any(p in txt for p in ["verify your identity", "unusual activity",
                                  "security check required", "prove you're not a bot"]):
            page.screenshot(path=f"captcha_debug_{int(time.time())}.png")
            set_captcha_lock()
            return "locked"
    except Exception:
        pass
    return "ok"

# ──────────────────────────────────────────────
# CACHE + DAILY LIMIT
# ──────────────────────────────────────────────
def text_hash(text):
    t = re.sub(r'[^\w\s]', '', re.sub(r'\s+', ' ', text.lower().strip()))[:250]
    return hashlib.sha256(t.encode()).hexdigest()[:16]

def load_cache(fp):
    if not os.path.exists(fp):
        return set()
    with open(fp, "r", encoding="utf-8") as f:
        return set(l.strip() for l in f if l.strip())

def save_to_cache(text, fp):
    with open(fp, "a", encoding="utf-8") as f:
        f.write(text_hash(text) + "\n")

def is_duplicate(text, cache):
    return text_hash(text) in cache

def trim_cache(fp, limit=500):
    if not os.path.exists(fp):
        return
    with open(fp, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > limit:
        with open(fp, "w", encoding="utf-8") as f:
            f.writelines(lines[-limit:])

def get_daily_limit():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(DAILY_LIMIT_FILE):
        try:
            with open(DAILY_LIMIT_FILE, "r") as f:
                d = json.load(f)
            if d.get("date") == today:
                return d["target"], d["count"]
        except Exception:
            pass
    target = random.randint(40, 48)
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump({"date": today, "target": target, "count": 0}, f)
    print(f"📊 New daily target: {target}")
    return target, 0

def increment_daily_counter():
    target, count = get_daily_limit()
    count += 1
    with open(DAILY_LIMIT_FILE, "w") as f:
        json.dump({"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   "target": target, "count": count}, f)
    print(f"📈 Daily count: {count}/{target}")
    return count >= target

# ──────────────────────────────────────────────
# TOPIC MEMORY (বাংলা stem সহ)
# ──────────────────────────────────────────────
STOPWORDS = {
    "এর", "এবং", "ও", "বা", "যে", "কি", "না", "হয়", "হলো", "ছিল", "আছে",
    "করে", "করছে", "করবেন", "করার", "বলেন", "বলেছেন", "জানান", "জানিয়েছেন",
    "থেকে", "জন্য", "দিয়ে", "নেয়া", "করা", "হওয়ার", "প্রতি", "সাথে", "উপর",
    "আগে", "পরে", "আর", "তবে", "যদি", "এই", "সেই", "এক", "দুই", "তিন",
    "জন", "দিন", "মাস", "বছর", "টাকা", "কোটি", "লাখ", "হাজার", "সব", "আজ",
    "the", "is", "at", "which", "on", "a", "an", "and", "or", "but", "in",
    "with", "to", "for", "of", "by", "from", "as", "not", "its", "that",
}

def stem(w):
    for suf in ["গুলো", "গুলোর", "দের", "দেরকে", "কে", "রে", "ের", "ে", "য়", "টি", "টা", "র", "ই"]:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w

def extract_keywords(text):
    words = re.findall(r'[\u0980-\u09FFa-zA-Z]+', text.lower())
    return {stem(w) for w in words if w not in STOPWORDS and len(w) > 2}

def load_topic_memory():
    if not os.path.exists(TOPIC_MEMORY_FILE):
        return []
    try:
        with open(TOPIC_MEMORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_topic_memory(mem):
    cutoff = time.time() - 6 * 3600
    mem = [m for m in mem if m["time"] > cutoff]
    with open(TOPIC_MEMORY_FILE, "w") as f:
        json.dump(mem, f)

def is_similar_topic(text, mem, min_overlap=2):
    kw = extract_keywords(text)
    return any(len(kw & set(m["keywords"])) >= min_overlap for m in mem)

def add_to_topic_memory(text):
    mem = load_topic_memory()
    mem.append({"time": time.time(), "keywords": list(extract_keywords(text))})
    save_topic_memory(mem)

# ──────────────────────────────────────────────
# ROBOTS.TXT
# ──────────────────────────────────────────────
_ROBOTS = {}
def robots_allow(base, url):
    if base not in _ROBOTS:
        rp = RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp.parse([])
        _ROBOTS[base] = rp
    return _ROBOTS[base].can_fetch(UA, url)

# ──────────────────────────────────────────────
# DISCOVERY: sitemap / RSS
# ──────────────────────────────────────────────
def fetch_text(url):
    try:
        r = requests.get(url, headers=HDR, timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"  ⚠️ fetch fail: {url} ({type(e).__name__})")
    return None

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None

def parse_sitemap(xml):
    soup = BeautifulSoup(xml, "html.parser")
    urls = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        lm = u.find("lastmod")
        urls.append((loc.get_text(strip=True),
                     parse_dt(lm.get_text(strip=True)) if lm else None))
    children = []
    for s in soup.find_all("sitemap"):
        loc = s.find("loc")
        if loc:
            children.append(loc.get_text(strip=True))
    return urls, children

def parse_rss(xml):
    soup = BeautifulSoup(xml, "html.parser")
    out = []
    for item in soup.find_all("item"):
        t = item.find("title")
        l = item.find("link")
        if not (t and l):
            continue
        link = l.get_text(strip=True) or (l.get("href") if l.has_attr("href") else None)
        if not link:
            continue
        d = item.find("pubdate")
        out.append((link, parse_dt(d.get_text(strip=True)) if d else None))
    return out

def get_source_urls(src):
    out = []
    if src["kind"] == "samakal":
        for delta in (0, 1):
            d = (datetime.now(BD_TZ) - timedelta(days=delta)).strftime("%Y-%m-%d")
            xml = fetch_text(f"https://samakal.com/sitemap/sitemap-daily-{d}.xml")
            if xml:
                out += parse_sitemap(xml)[0]
    elif src["kind"] == "rss":
        xml = fetch_text(src["maps"][0])
        if xml:
            out = parse_rss(xml)
    else:
        for m in src["maps"]:
            xml = fetch_text(m)
            if not xml:
                continue
            urls, children = parse_sitemap(xml)
            out += urls
            if children and not urls:
                for ch in children[:2]:
                    cxml = fetch_text(ch)
                    if cxml:
                        out += parse_sitemap(cxml)[0]
    return out

# ──────────────────────────────────────────────
# og:title + og:image
# ──────────────────────────────────────────────
def fetch_og(url):
    html = fetch_text(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    t = soup.find("meta", property="og:title")
    i = soup.find("meta", property="og:image")
    if not (t and i):
        return None
    title = (t.get("content") or "").strip()
    img = (i.get("content") or "").strip()
    if not title or not img:
        return None
    if img.startswith("//"):
        img = "https:" + img
    elif img.startswith("/"):
        img = urljoin(url, img)
    return title, img

def pick_article(posted_cache, failed):
    now = datetime.now(timezone.utc)
    for src in random.sample(SOURCES, len(SOURCES)):
        print(f"\n📡 {src['name']} checking...")
        cands = get_source_urls(src)
        random.shuffle(cands)
        seen = 0
        for link, dt in cands:
            if seen >= 10:
                break
            seen += 1
            if link in failed or is_duplicate(link, posted_cache):
                continue
            if dt and (now - dt).total_seconds() > 24 * 3600:
                continue
            if not robots_allow(src["base"], link):
                continue
            og = fetch_og(link)
            if not og:
                failed.add(link)
                continue
            title, img = og
            if len(title) < 10 or is_duplicate(title, posted_cache):
                continue
            if is_similar_topic(title, load_topic_memory()):
                print("  🔁 similar topic — skip")
                continue
            print(f"  ✅ picked: {title[:60]}")
            return {"title": title, "link": link, "image_url": img, "source": src["name"]}
    return None

# ──────────────────────────────────────────────
# IMAGE + CARD
# ──────────────────────────────────────────────
def download_image(url, fname, referer=None):
    headers = dict(HDR)
    headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=15)
        if r.status_code == 200:
            path = os.path.join(MEDIA_DIR, fname)
            with open(path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            return path
        print(f"  ⚠️ Image HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ Image error: {e}")
    return None

CARD_TEMPLATE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:1080px;height:1080px;background:#fff;font-family:'Hind Siliguri',sans-serif;overflow:hidden}
.imgwrap{width:1080px;height:640px;background:#111;position:relative}
.imgwrap img{width:100%;height:100%;object-fit:cover;display:block}
{% if logo_data_uri %}.logo{position:absolute;top:24px;left:24px;height:64px}{% endif %}
.body{padding:36px 48px;height:440px;display:flex;flex-direction:column}
.badge{align-self:flex-start;background:#111;color:#fff;font-size:30px;font-weight:600;padding:8px 24px;border-radius:6px;margin-bottom:24px}
.title{font-size:52px;line-height:1.35;font-weight:700;color:#111;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
.footer{margin-top:auto;display:flex;justify-content:space-between;color:#666;font-size:28px}
</style></head><body>
<div class="imgwrap"><img src="{{ image_data_uri }}">{% if logo_data_uri %}<img class="logo" src="{{ logo_data_uri }}">{% endif %}</div>
<div class="body">
<span class="badge">{{ category }}</span>
<div class="title">{{ title }}</div>
<div class="footer"><span>{{ date }}</span><span>📰</span></div>
</div></body></html>"""

def image_to_base64(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower()
    mime = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
    return f"data:{mime};base64,{data}"

def bengali_date_today():
    months = {1: "জানুয়ারি", 2: "ফেব্রুয়ারি", 3: "মার্চ", 4: "এপ্রিল", 5: "মে", 6: "জুন",
              7: "জুলাই", 8: "আগস্ট", 9: "সেপ্টেম্বর", 10: "অক্টোবর", 11: "নভেম্বর", 12: "ডিসেম্বর"}
    bd = ["০", "১", "২", "৩", "৪", "৫", "৬", "৭", "৮", "৯"]
    now = datetime.now(BD_TZ)
    day = "".join(bd[int(d)] for d in str(now.day))
    year = "".join(bd[int(d)] for d in str(now.year))
    return f"{day} {months[now.month]} {year}"

def create_news_card(title, img_path, out_path, category, date_str, logo_path=None):
    logo_uri = ""
    if logo_path and os.path.exists(logo_path):
        logo_uri = image_to_base64(logo_path)
    html = jinja2.Template(CARD_TEMPLATE_HTML).render(
        title=title, image_data_uri=image_to_base64(img_path),
        logo_data_uri=logo_uri, category=category, date=date_str)
    with open("temp_card.html", "w", encoding="utf-8") as f:
        f.write(html)
    headless = os.environ.get("HEADLESS", "false").lower() == "true"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        page = browser.new_page(viewport={"width": 1080, "height": 1080})
        page.goto(f"file://{os.path.abspath('temp_card.html')}")
        page.wait_for_load_state("networkidle")
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.screenshot(path=out_path, clip={"x": 0, "y": 0, "width": 1080, "height": 1080})
        browser.close()
    return out_path

# ──────────────────────────────────────────────
# HUMAN-LIKE MOUSE + TYPING
# ──────────────────────────────────────────────
def human_mouse_move(page, tx, ty, steps=15):
    sx, sy = random.randint(100, 300), random.randint(100, 300)
    cx = (sx + tx) / 2 + random.randint(-80, 80)
    cy = (sy + ty) / 2 + random.randint(-80, 80)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * tx
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ty
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.005, 0.015))

def human_type(element, text):
    element.click()
    time.sleep(random.uniform(0.3, 0.8))
    for ch in text:
        element.type(ch, delay=random.randint(40, 120))
        if random.random() < 0.05:
            time.sleep(random.uniform(0.3, 0.9))
    time.sleep(random.uniform(0.5, 1.2))

# ──────────────────────────────────────────────
# FACEBOOK POSTING (Create post → Next → Post)
# ──────────────────────────────────────────────
def post_to_facebook(page, caption, image_path):
    try:
        page.goto(f"https://www.facebook.com/profile.php?id={FB_PAGE_ID}",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(4000, 7000))
        status = check_fb_health(page)
        if status != "ok":
            return status

        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(300, 600))
            time.sleep(random.uniform(0.5, 1.2))

        # ১. কম্পোজার খোলা
        trigger = page.wait_for_selector(
            'div[role="button"][aria-label*="What\'s on your mind"]', timeout=20000)
        box = trigger.bounding_box()
        human_mouse_move(page, box['x'] + box['width'] // 2, box['y'] + box['height'] // 2)
        trigger.click()
        page.wait_for_timeout(random.randint(1500, 2500))
        page.wait_for_selector('div[role="dialog"]', timeout=15000)

        # ২. ক্যাপশন টাইপ
        tbox = page.wait_for_selector('div[role="dialog"] div[role="textbox"]', timeout=15000)
        human_type(tbox, caption)
        page.wait_for_timeout(random.randint(800, 1500))

        # ৩. ছবি যুক্ত করা
        photo_btn = page.wait_for_selector(
            'div[role="dialog"] div[aria-label="Photo/video"]', timeout=15000)
        with page.expect_file_chooser(timeout=15000) as fc:
            photo_btn.click()
        fc.value.set_files(image_path)
        print("  🖼️ card queued, waiting upload...")
        try:
            page.wait_for_selector('div[role="dialog"] [aria-label="Edit"]', timeout=20000)
            print("  ✅ upload preview confirmed")
        except Exception:
            page.wait_for_timeout(5000)
        page.wait_for_timeout(random.randint(2500, 4500))

        # ৪. Next
        try:
            next_btn = page.wait_for_selector(
                'div[role="dialog"] div[role="button"]:has(span:text-is("Next"))', timeout=10000)
        except Exception:
            next_btn = page.get_by_role("button", name="Next", exact=True)
        box = next_btn.bounding_box()
        human_mouse_move(page, box['x'] + box['width'] // 2, box['y'] + box['height'] // 2)
        next_btn.click()
        page.wait_for_timeout(random.randint(2000, 3500))

        # ৫. Post settings → Post
        post_btn = page.wait_for_selector(
            'div[role="dialog"] div[aria-label="Post"][role="button"]', timeout=15000)
        box = post_btn.bounding_box()
        human_mouse_move(page, box['x'] + box['width'] // 2, box['y'] + box['height'] // 2)
        post_btn.click()
        page.wait_for_timeout(random.randint(4000, 6000))

        if page.query_selector('div[role="dialog"]'):
            print("  ⚠️ dialog still open after Post")
            page.screenshot(path=f"fb_debug_{int(time.time())}.png")
            return "fail"
        return "ok"
    except Exception as e:
        print(f"  ❌ FB post error: {e}")
        try:
            page.screenshot(path=f"fb_debug_{int(time.time())}.png")
        except Exception:
            pass
        return "fail"

# ──────────────────────────────────────────────
# HUMAN DELAY (40-48 posts/day)
# ──────────────────────────────────────────────
def human_delay(hour):
    if 6 <= hour < 10:
        base = random.randint(22, 35) * 60
    elif 10 <= hour < 16:
        base = random.randint(25, 38) * 60
    elif 16 <= hour < 22:
        base = random.randint(22, 35) * 60
    else:
        base = random.randint(30, 45) * 60
    return base

ANTI_DETECT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Google Inc. (Intel)';
    if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter.call(this, parameter);
};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
);
"""

# ──────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────
def run_bot_loop():
    if not validate_session():
        return
    if is_captcha_locked():
        return
    target, current = get_daily_limit()
    if current >= target:
        print("🎯 Daily limit already reached.")
        return

    start_time = time.time()
    failed = set()
    logo_path = os.environ.get("LOGO_PATH", "logo.png")

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=headless, channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--use-gl=egl"])
        context = browser.new_context(
            storage_state=load_session(), user_agent=UA,
            viewport={'width': 1920, 'height': 1080}, locale="en-US")
        page = context.new_page()
        page.add_init_script(ANTI_DETECT)

        print(f"\n🤖 FB News Bot started — {datetime.now(BD_TZ).strftime('%Y-%m-%d %H:%M:%S')} (BD)")
        iteration = 0
        while True:
            target, current = get_daily_limit()
            if current >= target:
                print("🎯 Daily limit reached.")
                break
            if time.time() - start_time > MAX_DURATION - 300:
                print("⏰ 6h limit. Exiting.")
                break
            if is_captcha_locked():
                break

            iteration += 1
            print(f"\n🔄 Iteration {iteration} — {datetime.now(BD_TZ).strftime('%H:%M:%S')} (BD)")
            posted_cache = load_cache(POSTED_CACHE)
            art = pick_article(posted_cache, failed)

            if not art:
                print("⚠️ No new article. Sleeping 5m.")
                time.sleep(300)
                continue

            img_file = download_image(art["image_url"], "temp_news.jpg", referer=art["link"])
            if not img_file:
                failed.add(art["link"])
                time.sleep(60)
                continue

            create_news_card(art["title"], img_file, "card_output.jpg",
                             CARD_CATEGORY, bengali_date_today(), logo_path)
            print("🖼️ Card created")

            result = post_to_facebook(page, art["title"], "card_output.jpg")
            if result == "dead":
                print("🔐 Session dead — stopping run. নতুন কুকি আপলোড করুন।")
                break
            if result == "locked":
                break

            if result == "ok":
                save_to_cache(art["title"], POSTED_CACHE)
                save_to_cache(art["link"], POSTED_CACHE)
                add_to_topic_memory(art["title"])
                trim_cache(POSTED_CACHE)
                print("✅ Posted!")
                if increment_daily_counter():
                    break
                delay = human_delay(datetime.now(BD_TZ).hour)
            else:
                failed.add(art["link"])
                delay = random.randint(90, 180)
            print(f"⏳ Next in {delay // 60}m...")
            time.sleep(delay)

        browser.close()
    print("\n🔒 Browser closed. Loop ended.")

if __name__ == "__main__":
    delay = random.randint(60, 180)
    print(f"⏱ {delay}s initial delay...")
    time.sleep(delay)
    run_bot_loop()
