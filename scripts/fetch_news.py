import os, json, hashlib, time, datetime, urllib.parse
from urllib.parse import urlparse
import feedparser, requests

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-2.5-flash"
MAX_NEW = 6          # প্রতিবার সর্বোচ্চ কতটা নতুন পোস্ট
SEEN_FILE = "data/seen.json"

# খবরের বিষয় (Google News বাংলাদেশ সংস্করণে খোঁজা হবে)
QUERIES = [
    "বাংলাদেশ ক্রিকেট",
    "বাংলাদেশ ফুটবল",
    "বিপিএল",
    "আন্তর্জাতিক ক্রিকেট",
    "বিশ্বকাপ ফুটবল",
    "বাংলাদেশ জাতীয় দল",
]

# শুধু এই বাংলাদেশি সাইটগুলোর খবর নেওয়া হবে (ভারতীয় সাইট স্বয়ংক্রিয়ভাবে বাদ)
ALLOWED = [
    "prothomalo.com", "jagonews24.com", "kalerkantho.com", "jugantor.com",
    "samakal.com", "banglatribune.com", "dhakapost.com", "somoynews.tv",
    "ittefaq.com.bd", "bd-pratidin.com", "bdnews24.com", "dailystar.net",
    "tbsnews.net", "dhakatribune.com", "jamuna.tv", "ntvbd.com",
    "rtvonline.com", "kalbela.com", "banglanews24.com", "risingbd.com",
    "channel24bd.tv", "manobkantha.com.bd", "observerbd.com",
]


def allowed(href):
    host = (urlparse(href or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-2000:], f)


def collect():
    items, titles = [], set()
    for q in QUERIES:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q + " when:1d")
               + "&hl=bn&gl=BD&ceid=BD:bn")
        feed = feedparser.parse(url)
        for e in feed.entries[:15]:
            src = getattr(e, "source", {}) or {}
            if not allowed(src.get("href", "")):
                continue
            title = e.title.rsplit(" - ", 1)[0].strip()
            if title in titles:
                continue
            titles.add(title)
            items.append({
                "title": title,
                "link": e.link,
                "source": src.get("title", "সূত্র"),
            })
    return items


def pick_model():
    """এই Key দিয়ে যে মডেলগুলো চলে সেগুলো খুঁজে সবচেয়ে উপযুক্তটা বেছে নেয়"""
    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": API_KEY},
        timeout=30,
    )
    print("ListModels status:", r.status_code)
    if r.status_code != 200:
        print("ListModels body:", r.text[:400])
        return None
    names = [
        m["name"].split("/", 1)[1]
        for m in r.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    print("Available models:", names)
    flash = [n for n in names if "flash" in n and "lite" not in n
             and "image" not in n and "tts" not in n and "live" not in n]
    lite = [n for n in names if "flash" in n and "lite" in n]
    for group in (flash, lite, names):
        if group:
            return sorted(group, reverse=True)[0]
    return None


def rewrite(title):
    prompt = (
        "তুমি একজন বাংলা স্পোর্টস নিউজ এডিটর। নিচের হেডলাইন থেকে একটি সংক্ষিপ্ত নিউজ ব্রিফ লেখো।\n"
        "কঠোর নিয়ম: শুধু হেডলাইনে থাকা তথ্য ব্যবহার করবে। নতুন কোনো তথ্য, সংখ্যা, "
        "নাম, তারিখ বা উদ্ধৃতি বানাবে না। ২-৩ বাক্য, সহজ বাংলায়।\n"
        'আউটপুট শুধু JSON: {"title": "নতুন শিরোনাম", "body": "ব্রিফ"}\n\n'
        f"হেডলাইন: {title}"
    )
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def main():
    global MODEL
    picked = pick_model()
    if picked:
        MODEL = picked
    print("Using model:", MODEL)
    errors = 0
    seen = load_seen()
    os.makedirs("_posts", exist_ok=True)
    made = 0
    for it in collect():
        if made >= MAX_NEW:
            break
        h = hashlib.sha1(it["title"].encode("utf-8")).hexdigest()[:10]
        if h in seen:
            continue
        try:
            out = rewrite(it["title"])
        except Exception as ex:
            print("Gemini error:", ex)
            errors += 1
            if errors >= 3:
                print("Too many errors, stopping.")
                break
            time.sleep(5)
            continue
        errors = 0
        now = datetime.datetime.now(datetime.timezone.utc)
        fname = f"_posts/{now:%Y-%m-%d}-{h}.md"
        title = out["title"].replace('"', "'")
        body = out["body"].strip()
        with open(fname, "w", encoding="utf-8") as f:
            f.write(
                f'---\nlayout: post\ntitle: "{title}"\n'
                f"date: {now:%Y-%m-%d %H:%M:%S} +0000\n---\n\n"
                f"{body}\n\n"
                f"**সূত্র:** [{it['source']}]({it['link']})\n"
            )
        seen.add(h)
        made += 1
        print("Created:", fname)
        time.sleep(6)  # ফ্রি লিমিটের জন্য বিরতি
    save_seen(seen)
    print("Done. New posts:", made)


if __name__ == "__main__":
    main()
