"""Velora Studio - Creator Finder.

Button dabane par GitHub isay chalata hai. Yeh:
  1) TikTok Creative Center (USA/UK) se trending creators aur trending videos ke creators dhoondta hai
  2) followers ki had (maslan 2 lakh - 40 lakh) ke mutabiq chhaant'ta hai
  3) har creator ka naam / handle YouTube par check karta hai
  4) natija finder_state.json mein likhta hai (Velora app ka Finder tab wahi dikhata hai)

Yeh sirf dhoondta aur list banata hai. Kisi ki video download ya upload nahi karta.
"""
import os
import re
import json
import time
import difflib
import datetime
import traceback
from zoneinfo import ZoneInfo

import requests

import ytpost  # Google login aur password wala lock yahin se

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "yt_config.json")
STATE_PATH = os.path.join(ROOT, "finder_state.json")
START = time.time()
BUDGET = 25 * 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

CC_PAGES = {
    "creators": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/creator/pc/en?region={r}",
    "videos": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/pc/en?region={r}&period=7",
    "hashtags": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en?region={r}&period=7",
}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def time_left():
    return BUDGET - (time.time() - START)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Log:
    def __init__(self, state, tz):
        self.state, self.tz = state, tz

    def ts(self):
        return datetime.datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")

    def __call__(self, msg, level="info"):
        print(f"[{self.ts()}] {level.upper()} {msg}", flush=True)
        self.state.setdefault("log", []).insert(0, {"t": self.ts(), "l": level, "m": str(msg)[:400]})
        del self.state["log"][120:]


# ------------------------------------------------------------------ discovery
USER_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9._]{2,40})")


def harvest(obj, region, out):
    """Creative Center ke JSON jawab mein se creators nikalta hai (format badle to bhi chal jaye)."""
    if isinstance(obj, dict):
        user = ""
        for k in ("tt_link", "item_url", "share_url", "profile_url", "link"):
            m = USER_RE.search(str(obj.get(k) or ""))
            if m:
                user = m.group(1)
                break
        if not user:
            for k in ("unique_id", "uniqueId", "user_name", "username", "handle"):
                v = str(obj.get(k) or "").strip().lstrip("@")
                if re.fullmatch(r"[A-Za-z0-9._]{2,40}", v):
                    user = v
                    break
        if user:
            followers = 0
            for k in ("follower_cnt", "follower_count", "followerCount", "fans_count", "followers"):
                try:
                    followers = int(obj.get(k) or 0)
                except (TypeError, ValueError):
                    followers = 0
                if followers:
                    break
            nick = ""
            for k in ("nick_name", "nickname", "nickName", "author_name", "creator_name"):
                if obj.get(k):
                    nick = str(obj[k])[:80]
                    break
            c = out.setdefault(user.lower(), {"username": user, "nickname": "", "followers": 0, "regions": []})
            c["nickname"] = c["nickname"] or nick
            c["followers"] = max(c["followers"], followers)
            if region not in c["regions"]:
                c["regions"].append(region)
        for v in obj.values():
            harvest(v, region, out)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, region, out)


def discover(regions, log):
    """Headless browser se Creative Center kholta hai aur jo data page khud mangwata hai wo pakar leta hai."""
    from playwright.sync_api import sync_playwright
    found = {}
    stats = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
        for region in regions:
            for kind, tpl in CC_PAGES.items():
                if time_left() < 12 * 60:
                    break
                page = ctx.new_page()
                got = {"n": 0}

                def on_resp(resp, region=region, got=got):
                    try:
                        if "creative_radar_api" in resp.url and resp.status == 200:
                            data = resp.json()
                            before = len(found)
                            harvest(data, region, found)
                            got["n"] += len(found) - before
                    except Exception:
                        pass
                page.on("response", on_resp)
                try:
                    page.goto(tpl.format(r=region), wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(6000)
                    for _ in range(6):          # "View more" daba kar aur load karo
                        btn = page.locator("text=/View more/i").first
                        if btn.count() and btn.is_visible():
                            btn.click()
                            page.wait_for_timeout(3000)
                        else:
                            page.mouse.wheel(0, 4000)
                            page.wait_for_timeout(1500)
                    if got["n"] == 0:          # API na pakra gaya to page ke links se nikaal lo
                        html = page.content()
                        for u in set(USER_RE.findall(html)):
                            c = found.setdefault(u.lower(), {"username": u, "nickname": "", "followers": 0, "regions": []})
                            if region not in c["regions"]:
                                c["regions"].append(region)
                            got["n"] += 1
                except Exception as e:
                    log(f"{region} {kind}: page did not load - {str(e)[:120]}", "error")
                stats[f"{region} {kind}"] = got["n"]
                page.close()
        browser.close()
    log("found on TikTok Creative Center: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    return found


def tiktok_profile(username):
    """TikTok profile page se followers aur naam (agar Creative Center ne na diye hon)."""
    try:
        r = requests.get(f"https://www.tiktok.com/@{username}", headers={"User-Agent": UA, "Accept-Language": "en-US"},
                         timeout=25)
        if r.status_code != 200:
            return None, ""
        m = re.search(r'"followerCount":(\d+)', r.text)
        n = re.search(r'"nickname":"([^"]{1,80})"', r.text)
        nick = ""
        if n:
            try:
                nick = json.loads(f'"{n.group(1)}"')
            except Exception:
                nick = n.group(1)
        return (int(m.group(1)) if m else None), nick
    except Exception:
        return None, ""


# ------------------------------------------------------------------ YouTube check
class YT:
    def __init__(self, cfg, key, log):
        self.log = log
        self.searches_left = int((cfg.get("finder") or {}).get("search_budget") or 60)
        self.token = ""
        try:
            ctx = ytpost.Ctx(cfg, {})
            ctx.client_secret = (ytpost.decrypt_json(cfg["google_secret_enc"], key) or {}).get("secret", "")
            ctx.refresh = {str(k): v for k, v in ytpost.decrypt_json(cfg["tokens_enc"], key).items()}
            for ch in ctx.refresh:
                try:
                    self.token = ytpost.yt_access_token(ctx, ch)
                    break
                except Exception:
                    continue
        except Exception as e:
            log(f"Could not unlock Google login: {e}", "error")
        if not self.token:
            log("No working YouTube connection - connect a channel in the Channels tab first", "error")

    def get(self, path, params):
        r = requests.get("https://www.googleapis.com/youtube/v3/" + path, params=params,
                         headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        if r.status_code == 403 and "quota" in r.text.lower():
            raise RuntimeError("quota")
        r.raise_for_status()
        return r.json()

    def by_handle(self, username):
        d = self.get("channels", {"part": "snippet,statistics", "forHandle": "@" + username})
        it = (d.get("items") or [None])[0]
        return it

    def search(self, q):
        self.searches_left -= 1
        d = self.get("search", {"part": "snippet", "type": "channel", "q": q, "maxResults": 6})
        return d.get("items") or []


def check(yt, c):
    """exists = YouTube par isi naam/handle ka channel hai, similar = milta julta, not_found = nahi mila."""
    user, nick = c["username"], c.get("nickname") or ""
    it = yt.by_handle(user)
    if it:
        s = it.get("snippet", {})
        return "exists", [{"title": s.get("title", ""), "url": f"https://www.youtube.com/@{user}",
                           "subs": int(it.get("statistics", {}).get("subscriberCount") or 0)}]
    if yt.searches_left <= 0:
        return "pending", []
    names = [user] + ([nick] if len(norm(nick)) >= 4 and norm(nick) != norm(user) else [])
    matches, exact = [], False
    for item in yt.search(user):
        s = item.get("snippet", {})
        title = s.get("title") or s.get("channelTitle") or ""
        cid = (item.get("id") or {}).get("channelId") or s.get("channelId") or ""
        nt = norm(title)
        if not nt or not cid:
            continue
        is_exact = any(nt == norm(n) for n in names)
        ratio = max(difflib.SequenceMatcher(None, nt, norm(n)).ratio() for n in names)
        contains = any(len(norm(n)) >= 5 and (norm(n) in nt or nt in norm(n)) for n in names)
        if is_exact or ratio >= 0.8 or contains:
            exact = exact or is_exact
            matches.append({"title": title, "url": f"https://www.youtube.com/channel/{cid}", "exact": is_exact})
    if exact:
        return "exists", matches[:3]
    if matches:
        return "similar", matches[:3]
    return "not_found", []


# ------------------------------------------------------------------ main
def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    fcfg = cfg.get("finder") or {}
    tz = ZoneInfo(cfg.get("timezone") or "Asia/Karachi")
    log = Log(state, tz)
    regions = [r for r in (fcfg.get("regions") or ["US", "GB"]) if r in ("US", "GB", "CA", "AU")] or ["US"]
    lo = int(fcfg.get("min_followers") or 200_000)
    hi = int(fcfg.get("max_followers") or 4_000_000)
    want = int(fcfg.get("max_results") or 30)
    creators = state.setdefault("creators", {})
    hidden = set(x.lower() for x in (cfg.get("finder_hidden") or []))
    state["running"] = True
    try:
        found = discover(regions, log)
        # pehle se check kiye hue (aur 'pending' wale) bhi dobara line mein
        for k, c in creators.items():
            if c.get("status") == "pending" and k not in found:
                found[k] = {"username": c["username"], "nickname": c.get("nickname", ""),
                            "followers": c.get("followers", 0), "regions": c.get("regions", [])}
        yt = YT(cfg, os.environ.get("APP_KEY", ""), log)
        done = new_green = 0
        queue = sorted(found.values(), key=lambda c: -int(c.get("followers") or 0))
        for c in queue:
            key = c["username"].lower()
            old = creators.get(key)
            if key in hidden or (old and old.get("status") not in ("pending", None)):
                continue
            if time_left() < 3 * 60 or done >= want * 3:
                break
            if not c.get("followers") or not c.get("nickname"):
                f, nick = tiktok_profile(c["username"])
                c["followers"] = c.get("followers") or f or 0
                c["nickname"] = c.get("nickname") or nick
            if c["followers"] and not (lo <= c["followers"] <= hi):
                continue
            if not yt.token:
                break
            try:
                status, matches = check(yt, c)
            except RuntimeError:
                log("Google search limit for today reached - the rest will be checked tomorrow", "error")
                break
            except Exception as e:
                log(f"@{c['username']}: YouTube check failed - {str(e)[:120]}", "error")
                continue
            creators[key] = {**c, "status": status, "matches": matches, "checked": log.ts(),
                             "tiktok": f"https://www.tiktok.com/@{c['username']}"}
            done += 1
            new_green += status == "not_found"
        log(f"done: {done} creators checked, {new_green} not found on YouTube"
            + (f", {yt.searches_left} searches left" if yt.token else ""), "success" if done else "info")
    except Exception as e:
        log(f"Finder error: {e} | {traceback.format_exc()[-300:]}", "error")
    # purane natije kam karo (aakhri 1500)
    if len(creators) > 1500:
        for k in sorted(creators, key=lambda k: creators[k].get("checked", ""))[:len(creators) - 1500]:
            del creators[k]
    state["running"] = False
    state["last_run"] = log.ts()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
