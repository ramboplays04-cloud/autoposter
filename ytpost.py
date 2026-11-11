"""Velora Studio - YouTube engine.

GitHub Actions isay har ~30 minute baad chalata hai. Yeh:
  1) har account ke TikTok ko check kar ke videos ki line (queue) banata hai
  2) schedule ka time aane par video download kar ke YouTube channel par Short upload karta hai
  3) sab kuch yt_state.json mein likhta hai (iPhone app wahi parhti hai)

Settings yt_config.json mein hain (Velora Studio app se badalti hain).
Tokens app ke password (GitHub Secret APP_KEY) se locked rehte hain.
"""
import os
import re
import json
import time
import base64
import datetime
import tempfile
import traceback
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "yt_config.json")
STATE_PATH = os.path.join(ROOT, "yt_state.json")
TIMEOUT = 60
UPLOAD_TIMEOUT = 60 * 20
RUN_BUDGET = 45 * 60          # GitHub job 55 min ka hai; 45 min ke baad naya upload shuru nahi karte
START = time.time()

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


# ------------------------------------------------------------------ helpers
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def time_left():
    return RUN_BUDGET - (time.time() - START)


def decrypt_json(enc, password):
    """App data ko password se lock (AES-GCM, PBKDF2 200k) kar ke rakhti hai - wahi format yahan khulta hai."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    raw = base64.b64decode(enc)
    salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000).derive(password.encode())
    return json.loads(AESGCM(key).decrypt(iv, ct, None).decode())


def parse_times(items):
    out = []
    if isinstance(items, str):
        items = re.split(r"[,\s]+", items)
    for p in items or []:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(p).strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
    return sorted(set(out))


def username_from_url(url):
    m = re.search(r"tiktok\.com/@([^/?#]+)", url or "")
    return m.group(1) if m else (url or "").lstrip("@")


def normalize_url(text):
    t = (text or "").strip()
    if t.startswith("http"):
        return t.split("?")[0].rstrip("/")
    return f"https://www.tiktok.com/@{t.lstrip('@')}"


HASHTAG = re.compile(r"(?<!\w)#([^\s#]+)")


def clean_text(s):
    s = (s or "").replace("<", "").replace(">", "")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def build_title(caption, acc, n):
    """YouTube title: max 100 characters, '<' '>' allowed nahi."""
    base = clean_text(HASHTAG.sub("", caption or ""))
    base = re.sub(r"\s+", " ", base).strip()
    tpl = (acc.get("title_template") or "").strip()
    if tpl:
        title = tpl.replace("{caption}", base).replace("{n}", str(n))
    else:
        title = base
    title = clean_text(title) or clean_text(acc.get("fallback_title") or "") or "New video"
    tag = " #Shorts" if acc.get("shorts_tag", True) and "#shorts" not in title.lower() else ""
    limit = 100 - len(tag)
    if len(title) > limit:
        title = title[:limit - 1].rstrip() + "…"
    return title + tag


def build_description(caption, acc):
    text = caption if acc.get("use_caption", True) else ""
    if text and acc.get("remove_hashtags"):
        text = HASHTAG.sub("", text)
    text = clean_text(text)
    extra = clean_text(acc.get("extra") or "")
    if extra:
        text = f"{text}\n\n{extra}" if text else extra
    return text[:4900]


def build_tags(caption):
    tags, total = [], 0
    for t in HASHTAG.findall(caption or ""):
        t = clean_text(t).strip(",")
        if not t or t.lower() in (x.lower() for x in tags):
            continue
        if total + len(t) + 1 > 450:
            break
        tags.append(t)
        total += len(t) + 1
    return tags


# ------------------------------------------------------------------ context
class Ctx:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.tz = ZoneInfo(cfg.get("timezone") or "Asia/Karachi")
        self.now = datetime.datetime.now(self.tz)
        self.client_id = cfg.get("google_client_id") or ""
        self.client_secret = ""
        self.refresh = {}        # channel_id -> refresh token
        self.access = {}         # channel_id -> access token (is run ke liye)
        self.bad_channels = set()
        self.quota_hit = False
        self.scanned = set()
        self.proxy = ""

    def ts(self):
        return datetime.datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")

    def today(self):
        return datetime.datetime.now(self.tz).date().isoformat()

    def log(self, msg, level="info", acc=None):
        who = ("@" + acc["username"] + ": ") if acc else ""
        print(f"[{self.ts()}] {level.upper()} {who}{msg}", flush=True)
        self.state.setdefault("log", []).insert(0, {
            "t": self.ts(), "l": level, "a": acc["id"] if acc else "", "m": str(msg)[:400]})
        del self.state["log"][300:]

    def acc_state(self, acc):
        st = self.state.setdefault("accounts", {}).setdefault(acc["id"], {})
        st.setdefault("videos", [])
        st.setdefault("seen", [])
        st.setdefault("slots", {})
        st.setdefault("owed", 0)
        st.setdefault("last_checked", None)
        st.setdefault("first_done", False)
        return st

    def used(self):
        return self.state.setdefault("used", [])

    def uploads_today(self, channel_id):
        day = self.state.setdefault("daily", {}).setdefault(self.today(), {})
        return int(day.get(channel_id) or 0)

    def count_upload(self, channel_id):
        daily = self.state.setdefault("daily", {})
        day = daily.setdefault(self.today(), {})
        day[channel_id] = int(day.get(channel_id) or 0) + 1
        for d in sorted(daily)[:-3]:
            del daily[d]


# ------------------------------------------------------------------ TikTok
def _ydl(ctx, opts):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    cookies = os.path.join(ROOT, "cookies.txt")
    if os.path.isfile(cookies):
        base["cookiefile"] = cookies
    if ctx.proxy:
        base["proxy"] = ctx.proxy
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def tiktok_list(ctx, url, limit):
    with _ydl(ctx, {"extract_flat": "in_playlist", "skip_download": True, "playlistend": int(limit)}) as y:
        info = y.extract_info(url, download=False)
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    user = username_from_url(url)
    out = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        vid = str(e["id"])
        vurl = e.get("webpage_url") or e.get("url") or ""
        if not vurl.startswith("http"):
            vurl = f"https://www.tiktok.com/@{user}/video/{vid}"
        out.append({"id": vid, "url": vurl, "caption": e.get("description") or e.get("title") or "",
                    "views": int(e.get("view_count") or 0)})
    return out


def tiktok_download(ctx, url, folder):
    # Watermark wali copy (format id mein "download") ko kabhi nahi chunte
    fmt = ("best[ext=mp4][vcodec!=none][acodec!=none][format_id!*=download]/"
           "best[ext=mp4][format_id!*=download]/best[format_id!*=download]/best")
    with _ydl(ctx, {"outtmpl": os.path.join(folder, "%(id)s.%(ext)s"), "format": fmt,
                    "noplaylist": True, "retries": 5}) as y:
        info = y.extract_info(url, download=True)
        req = info.get("requested_downloads") or []
        path = (req[0].get("filepath") if req else None) or y.prepare_filename(info)
    if not path or not os.path.isfile(path):
        raise RuntimeError("File not found after download")
    return path, info.get("description") or info.get("title") or ""


# ------------------------------------------------------------------ YouTube
class ReauthError(Exception):
    pass


class QuotaError(Exception):
    pass


def _google_error(resp):
    try:
        err = resp.json().get("error")
    except Exception:
        return resp.status_code, "", resp.text[:200]
    if isinstance(err, str):            # token endpoint
        return resp.status_code, err, err
    err = err or {}
    reasons = [e.get("reason", "") for e in err.get("errors", [])]
    return resp.status_code, (reasons[0] if reasons else ""), err.get("message", "")[:200]


def yt_access_token(ctx, channel_id):
    if channel_id in ctx.access:
        return ctx.access[channel_id]
    rt = ctx.refresh.get(channel_id)
    if not rt:
        raise ReauthError("this channel is not connected (Channels tab → Connect a channel)")
    r = requests.post(TOKEN_URL, data={"client_id": ctx.client_id, "client_secret": ctx.client_secret,
                                       "refresh_token": rt, "grant_type": "refresh_token"}, timeout=TIMEOUT)
    if r.status_code >= 400:
        code, reason, msg = _google_error(r)
        if reason in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise ReauthError(f"Google says '{reason}'. Reconnect this channel in the app (Channels tab).")
        raise RuntimeError(f"Google token error ({code}): {msg}")
    ctx.access[channel_id] = r.json()["access_token"]
    return ctx.access[channel_id]


def yt_upload(ctx, channel_id, path, title, description, tags, acc):
    token = yt_access_token(ctx, channel_id)
    size = os.path.getsize(path)
    meta = {
        "snippet": {"title": title, "description": description, "tags": tags,
                    "categoryId": str(acc.get("category") or "22")},
        "status": {"privacyStatus": acc.get("privacy") or "public",
                   "selfDeclaredMadeForKids": bool(acc.get("kids", False)),
                   "embeddable": True},
    }
    notify = "true" if acc.get("notify", True) else "false"
    init = requests.post(
        f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status&notifySubscribers={notify}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
        data=json.dumps(meta), timeout=TIMEOUT)
    if init.status_code >= 400:
        code, reason, msg = _google_error(init)
        if code == 401:
            ctx.access.pop(channel_id, None)
            raise ReauthError("Google login expired. Reconnect this channel in the app.")
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise QuotaError(f"Google daily limit reached ({reason}). Uploads continue automatically tomorrow.")
        if reason in ("uploadLimitExceeded",):
            raise QuotaError("YouTube says this channel uploaded too much today. It will continue tomorrow.")
        raise RuntimeError(f"YouTube error ({code} {reason}): {msg}")
    location = init.headers.get("Location")
    if not location:
        raise RuntimeError("YouTube did not return an upload address")
    last = None
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                up = requests.put(location, data=f, timeout=UPLOAD_TIMEOUT,
                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                                           "Content-Length": str(size)})
            if up.status_code in (200, 201):
                return up.json()["id"]
            code, reason, msg = _google_error(up)
            last = f"YouTube upload error ({code} {reason}): {msg}"
            if code < 500:
                break
        except requests.RequestException as e:
            last = f"network error while uploading: {e}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(last or "upload failed")


# ------------------------------------------------------------------ core
def scan(ctx, acc):
    st = ctx.acc_state(acc)
    ctx.scanned.add(acc["id"])
    first = not st["first_done"]
    limit = int(ctx.cfg.get("scan_limit") or 30)
    if first and acc.get("import_old", True):
        limit = max(limit, int(ctx.cfg.get("first_import") or 200))
    try:
        items = tiktok_list(ctx, normalize_url(acc["tiktok"]), limit)
    except Exception as e:
        ctx.log(f"TikTok check failed - {e}", "error", acc)
        st["last_checked"] = ctx.ts()
        return 0
    seen, used = set(st["seen"]), set(ctx.used())
    new = 0
    for it in reversed(items):          # purani pehle, nayi aakhir mein
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        st["seen"].append(it["id"])
        if it["id"] in used:
            status = "duplicate"         # yeh video kisi aur channel par ja chuki hai
        elif first and not acc.get("import_old", True):
            status = "skipped"
        else:
            status = "new"
        st["videos"].append({"id": it["id"], "url": it["url"], "caption": it["caption"][:500],
                             "views": it["views"], "status": status, "attempts": 0, "found": ctx.ts()})
        new += status == "new"
    st["first_done"] = True
    st["last_checked"] = ctx.ts()
    if first and not acc.get("import_old", True):
        ctx.log(f"first check: skipped {len(items)} old videos, only new ones will be uploaded", "info", acc)
    else:
        ctx.log(f"check done: {new} new videos added to queue", "info", acc)
    return new


def pick_next(ctx, acc, st):
    used = set(ctx.used())
    open_ = [v for v in st["videos"] if v["status"] in ("new", "retry")]
    for v in open_:
        if v["id"] in used:
            v["status"] = "duplicate"
    open_ = [v for v in open_ if v["status"] != "duplicate"]
    if not open_:
        return None
    order = acc.get("order") or "oldest"
    if order == "newest":
        return open_[-1]
    if order == "views":
        return max(open_, key=lambda v: int(v.get("views") or 0))
    return open_[0]


def process(ctx, acc, video):
    """True = upload ho gaya, False = fail (dobara try hoga), None = setup ka masla (is run mein ruk jao)."""
    ch = str(acc.get("channel_id") or "")
    if not ch:
        ctx.log("no YouTube channel selected for this account (Edit the account)", "error", acc)
        return None
    if ch in ctx.bad_channels or ctx.quota_hit:
        return None
    cap = int(ctx.cfg.get("daily_cap") or 6)
    if ctx.uploads_today(ch) >= cap:
        ctx.log(f"safety limit: {cap} uploads today on this channel, the rest continue tomorrow", "info", acc)
        return None
    max_attempts = int(ctx.cfg.get("max_attempts") or 3)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, cap_text = tiktok_download(ctx, video["url"], tmp)
            cap_text = cap_text or video.get("caption") or ""
            video["caption"] = cap_text[:500]
            n = sum(1 for v in ctx.acc_state(acc)["videos"] if v["status"] == "uploaded") + 1
            yt_id = yt_upload(ctx, ch, path, build_title(cap_text, acc, n), build_description(cap_text, acc),
                              build_tags(cap_text), acc)
            video.update(status="uploaded", yt=yt_id, uploaded=ctx.ts(), error="")
            ctx.used().append(video["id"])
            ctx.count_upload(ch)
            ctx.state.setdefault("channels_status", {})[ch] = {"ok": True, "t": ctx.ts()}
            ch_name = next((c.get("title") for c in ctx.cfg.get("channels", []) if c.get("id") == ch), ch)
            ctx.log(f"video {video['id'][-6:]} -> {ch_name} uploaded (youtu.be/{yt_id})", "success", acc)
            return True
        except ReauthError as e:
            ctx.bad_channels.add(ch)
            ctx.state.setdefault("channels_status", {})[ch] = {"ok": False, "error": str(e)[:200], "t": ctx.ts()}
            ctx.log(str(e), "error", acc)
            return None
        except QuotaError as e:
            ctx.quota_hit = True
            ctx.log(str(e), "error", acc)
            return None
        except Exception as e:
            video["attempts"] = int(video.get("attempts") or 0) + 1
            video["status"] = "retry" if video["attempts"] < max_attempts else "failed"
            video["error"] = str(e)[:300]
            ctx.log(f"video {video['id'][-6:]} fail ({video['attempts']}/{max_attempts}) - {e}", "error", acc)
            return False


def post_next(ctx, acc, count):
    st = ctx.acc_state(acc)
    done = tries = 0
    while done < count and tries < count + 3:
        if time_left() < 4 * 60:
            ctx.log("time limit of this run reached, the next run continues", "info", acc)
            break
        tries += 1
        video = pick_next(ctx, acc, st)
        if not video:
            if acc["id"] not in ctx.scanned and scan(ctx, acc) > 0:
                continue
            ctx.log("no new videos in queue", "info", acc)
            return done, True
        ok = process(ctx, acc, video)
        if ok is None:
            break
        if ok:
            done += 1
    return done, False


def trim(st):
    keep_open = [v for v in st["videos"] if v["status"] in ("new", "retry")]
    others = [v for v in st["videos"] if v["status"] not in ("new", "retry")][-200:]
    order = {id(v): i for i, v in enumerate(st["videos"])}
    st["videos"] = sorted(keep_open + others, key=lambda v: order[id(v)])
    st["seen"] = st["seen"][-5000:]
    for day in sorted(st["slots"])[:-3]:
        del st["slots"][day]


def auto(ctx, acc):
    st = ctx.acc_state(acc)
    today = ctx.now.date().isoformat()
    hhmm = ctx.now.strftime("%H:%M")
    slots = parse_times(acc.get("times"))
    per = max(1, min(5, int(acc.get("per_slot") or 1)))
    done = st["slots"].setdefault(today, [])
    new_account = st["last_checked"] is None

    every = int(ctx.cfg.get("check_minutes") or 60)
    due = True
    if st["last_checked"]:
        try:
            last = datetime.datetime.strptime(st["last_checked"], "%Y-%m-%d %H:%M").replace(tzinfo=ctx.tz)
            due = (ctx.now - last).total_seconds() >= (every - 5) * 60
        except ValueError:
            pass
    if due and time_left() > 10 * 60:
        scan(ctx, acc)

    passed = [s for s in slots if s <= hhmm]
    pending = [s for s in passed if s not in done]
    if new_account:          # naya account: aaj ke guzre slots par ek dum upload na karo
        done.extend(pending)
        return
    if pending:
        done.extend(pending)
        st["owed"] = int(st.get("owed") or 0) + per * len(pending)
        ctx.log(f"{pending[-1]} slot - {per} video(s) to upload", "info", acc)
    st["owed"] = min(int(st.get("owed") or 0), per * max(1, len(slots)))   # kabhi dher na lage
    if st["owed"] > 0 and not ctx.quota_hit:
        n, empty = post_next(ctx, acc, st["owed"])
        st["owed"] = 0 if empty else max(0, st["owed"] - n)


# ------------------------------------------------------------------ main
def main():
    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    ctx = Ctx(cfg, state)
    action = (os.environ.get("ACTION") or "auto").strip() or "auto"
    target = (os.environ.get("ACCOUNT") or "").strip()
    video_arg = (os.environ.get("VIDEO") or "").strip()

    accounts = []
    for a in cfg.get("accounts", []):
        a = dict(a)
        a["username"] = username_from_url(normalize_url(a.get("tiktok", "")))
        accounts.append(a)

    key = os.environ.get("APP_KEY", "")
    try:
        if cfg.get("google_secret_enc"):
            ctx.client_secret = (decrypt_json(cfg["google_secret_enc"], key) or {}).get("secret", "")
        if cfg.get("tokens_enc"):
            ctx.refresh = {str(k): v for k, v in decrypt_json(cfg["tokens_enc"], key).items()}
        if cfg.get("proxy_enc"):
            ctx.proxy = (decrypt_json(cfg["proxy_enc"], key) or {}).get("proxy", "").strip()
    except Exception:
        ctx.log("Could not unlock saved logins: GitHub Secret 'APP_KEY' must match the app password", "error")
    if ctx.proxy:
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = ctx.proxy
        try:
            ip = requests.get("https://api.ipify.org", timeout=20).text.strip()
            state["last_ip"] = ip
        except Exception as e:
            ctx.log(f"Fixed IP server is not reachable - {e}. Uploads stopped this run to keep your IP the same.", "error")
            ctx.quota_hit = True
    else:
        state.pop("last_ip", None)
    if not ctx.client_id or not ctx.client_secret:
        ctx.log("Google is not set up yet (Settings → Google app)", "error")
    elif not ctx.refresh:
        ctx.log("No YouTube channel connected yet (Channels tab)", "error")

    try:
        if action == "auto":
            for acc in accounts:
                if acc.get("enabled", True):
                    auto(ctx, acc)
        else:
            sel = [a for a in accounts if a["id"] == target] if target else accounts
            if not sel:
                ctx.log(f"account '{target}' not found", "error")
            for acc in sel:
                st = ctx.acc_state(acc)
                vid = next((v for v in st["videos"] if v["id"] == video_arg), None)
                if action == "scan":
                    scan(ctx, acc)
                elif action == "upload_now":
                    if st["last_checked"] is None:
                        scan(ctx, acc)
                    post_next(ctx, acc, 1)
                elif action == "upload_video" and vid:
                    process(ctx, acc, vid)
                elif action == "retry" and vid:
                    vid.update(status="new", attempts=0, error="")
                elif action == "skip" and vid:
                    vid["status"] = "skipped"
    except Exception as e:
        ctx.log(f"Engine error: {e} | {traceback.format_exc()[-300:]}", "error")

    ids = {a["id"] for a in accounts}
    for aid in list(state.get("accounts", {})):
        if aid not in ids:
            del state["accounts"][aid]
    for st in state.get("accounts", {}).values():
        trim(st)
    state["used"] = ctx.used()[-20000:]
    state["last_run"] = ctx.ts()
    state["last_action"] = action
    save_state(state)


if __name__ == "__main__":
    main()
