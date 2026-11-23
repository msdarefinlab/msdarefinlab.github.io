import os, re, json, hashlib, time, datetime, urllib.parse
from urllib.parse import urlparse
import feedparser, requests

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-flash-lite-latest"
MAX_NEW = 5          # প্রতিবার সর্বোচ্চ কতটা নতুন পোস্ট
SEEN_FILE = "data/seen.json"

QUERIES = [
    "বাংলাদেশ ক্রিকেট", "বাংলাদেশ ফুটবল", "বিপিএল", "বাংলাদেশ জাতীয় দল",
    "আন্তর্জাতিক ক্রিকেট", "টেস্ট ওয়ানডে টি-টোয়েন্টি", "ফুটবল লিগ",
    "বিশ্বকাপ ফুটবল", "নারী ক্রিকেট", "খেলার খবর",
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

PREFERRED = [
    "gemini-flash-lite-latest", "gemini-flash-latest",
    "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite",
]

# সংবাদ মেলানোর সময় বাদ দেওয়া সাধারণ শব্দ
STOP = set("""ও এবং এর করে হবে থেকে জন্য সঙ্গে নিয়ে দিয়ে কী কি না যে এই সেই তার তাদের
আজ আজকের হলো হয়েছে হয় করল করেছে দিল দেবে পারে বাংলাদেশ বাংলাদেশের ক্রিকেট ফুটবল দল দলের
ম্যাচ ম্যাচে খেলা খেলার বিশ্বকাপ টেস্ট ওয়ানডে সিরিজ জয় হার একটি একটা""".split())


def domain(href):
    h = (urlparse(href or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def allowed(href):
    h = domain(href)
    return any(h == d or h.endswith("." + d) for d in ALLOWED)


SUFFIXES = ["দেরকে", "দের", "ের", "কে", "তে", "য়", "র", "ে"]


def stem(w):
    """বাংলা বিভক্তি ছেঁটে মূল শব্দ বের করে (মোসাদ্দেককে -> মোসাদ্দেক)"""
    for suf in SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def tokens(t):
    t = re.sub(r"[\"'‘’“”।,.:;!?()\[\]{}\-–—|/]", " ", t)
    return {stem(w) for w in t.split() if len(w) > 1 and w not in STOP}


def similar(a, b):
    inter = len(a & b)
    return inter >= 2 and inter / max(1, min(len(a), len(b))) >= 0.45


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    os.makedirs("data", exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-3000:], f)


def hid(title):
    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]


def collect():
    items = []
    for q in QUERIES:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(q + " when:1d")
               + "&hl=bn&gl=BD&ceid=BD:bn")
        feed = feedparser.parse(url)
        rank = 0
        for e in feed.entries[:20]:
            src = getattr(e, "source", {}) or {}
            href = src.get("href", "")
            if not allowed(href):
                continue
            title = e.title.rsplit(" - ", 1)[0].strip()
            items.append({
                "title": title, "link": e.link,
                "source": src.get("title") or domain(href),
                "domain": domain(href), "rank": rank, "toks": tokens(title),
            })
            rank += 1
    return items


def build_clusters(items):
    """একই ঘটনার খবর একাধিক সাইটে থাকলে সেগুলো এক দলে রাখে। যত বেশি সাইটে, তত ট্রেন্ডিং।"""
    clusters = []
    for it in items:
        for c in clusters:
            if similar(it["toks"], c["rep"]):
                if it["domain"] not in {x["domain"] for x in c["items"]}:
                    c["items"].append(it)
                c["best"] = min(c["best"], it["rank"])
                break
        else:
            clusters.append({"rep": it["toks"], "items": [it], "best": it["rank"]})
    clusters.sort(key=lambda c: (-len(c["items"]), c["best"]))
    return clusters


def gemini(prompt, model=None, json_mode=True, timeout=60):
    cfg = {"responseMimeType": "application/json"} if json_mode else {}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model or MODEL}:generateContent",
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
        timeout=timeout,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def pick_model():
    for m in PREFERRED:
        try:
            gemini("একটি শব্দ লেখো: খেলা", model=m, json_mode=False, timeout=40)
            print("Probe OK:", m)
            return m
        except Exception as ex:
            print(f"Probe {m} failed:", str(ex)[:160].replace("\n", " "))
            time.sleep(2)
    return None


def rewrite(cluster):
    lines = "\n".join(f"- {i['source']}: {i['title']}" for i in cluster["items"])
    prompt = f"""তুমি বাংলাদেশের একটি স্পোর্টস নিউজ পোর্টালের অভিজ্ঞ সম্পাদক। নিচে একই ঘটনা নিয়ে এক বা একাধিক সংবাদমাধ্যমের হেডলাইন দেওয়া আছে।

{lines}

এগুলো থেকে একটি আকর্ষণীয় সংক্ষিপ্ত নিউজ ব্রিফ লেখো।

কঠোর নিয়ম:
১. শুধু উপরের হেডলাইনগুলোতে থাকা তথ্য ব্যবহার করবে। নতুন কোনো তথ্য, সংখ্যা, নাম, তারিখ, স্কোর বা উদ্ধৃতি বানাবে না।
২. প্রথম বাক্যেই মূল খবর বলবে। একই শব্দ বা বাক্য পুনরাবৃত্তি করবে না।
৩. ৩-৪টি বাক্য, সহজ ও স্বাভাবিক সংবাদের ভাষায়।
৪. শিরোনাম আকর্ষণীয় হবে কিন্তু ক্লিকবেইট বা অতিরঞ্জিত নয়।
৫. cat হবে ঠিক এই তিনটির একটি: "ক্রিকেট", "ফুটবল" অথবা "অন্যান্য"।

আউটপুট শুধু JSON:
{{"title": "শিরোনাম", "teaser": "এক লাইনের আকর্ষণীয় সারাংশ (সর্বোচ্চ ৯০ অক্ষর)", "body": "ব্রিফ", "cat": "ক্রিকেট"}}"""
    out = json.loads(gemini(prompt))
    if isinstance(out, list):
        out = out[0]
    for k in ("title", "teaser", "body", "cat"):
        out[k] = str(out.get(k, "")).strip()
    if not out["title"] or not out["body"]:
        raise ValueError("empty output")
    return out


def main():
    global MODEL
    picked = pick_model()
    if not picked:
        print("কোনো মডেল কাজ করেনি (কোটা বা Key সমস্যা)। পরে আবার চেষ্টা হবে।")
        return
    MODEL = picked
    print("Using model:", MODEL)

    seen = load_seen()
    os.makedirs("_posts", exist_ok=True)
    clusters = build_clusters(collect())
    print("Clusters found:", len(clusters))

    made, errors = 0, 0
    for cl in clusters:
        if made >= MAX_NEW:
            break
        ids = [hid(i["title"]) for i in cl["items"]]
        if any(i in seen for i in ids):
            continue
        try:
            out = rewrite(cl)
        except Exception as ex:
            print("Gemini error:", str(ex)[:200])
            errors += 1
            if errors >= 3:
                print("Too many errors, stopping.")
                break
            time.sleep(8)
            continue
        errors = 0
        cat = {"ক্রিকেট": "cricket", "ফুটবল": "football"}.get(out["cat"], "other")
        cat_bn = out["cat"] if out["cat"] in ("ক্রিকেট", "ফুটবল") else "খেলা"
        sources = [{"name": i["source"], "url": i["link"]} for i in cl["items"]]
        now = datetime.datetime.now(datetime.timezone.utc)
        fname = f"_posts/{now:%Y-%m-%d}-{ids[0]}.md"
        j = lambda v: json.dumps(v, ensure_ascii=False)
        with open(fname, "w", encoding="utf-8") as f:
            f.write(
                "---\nlayout: post\n"
                f"title: {j(out['title'])}\n"
                f"teaser: {j(out['teaser'])}\n"
                f"cat: {cat}\ncat_bn: {j(cat_bn)}\n"
                f"trending: {'true' if len(sources) >= 2 else 'false'}\n"
                f"source_count: {len(sources)}\n"
                f"sources: {j(sources)}\n"
                f"date: {now:%Y-%m-%d %H:%M:%S} +0000\n---\n\n"
                f"{out['body']}\n"
            )
        seen.update(ids)
        made += 1
        print(f"Created: {fname}  ({len(sources)} sources)")
        time.sleep(6)

    save_seen(seen)
    print("Done. New posts:", made)


if __name__ == "__main__":
    main()
