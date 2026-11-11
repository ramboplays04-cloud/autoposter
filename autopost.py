"""Faisal Auto Poster - Cloud engine.
GitHub Actions isay har ~30 minute baad khud chalata hai. Yeh:
  1) TikTok accounts check karta hai aur nayi videos queue mein daalta hai
  2) schedule ka time aane par video download kar ke Facebook Page par upload karta hai
  3) sab kuch state.json mein likh deta hai (iPhone app wahi parhti hai)
Settings config.json mein hain (iPhone app se badalti hain).
"""
import os
import re
import json
import base64
import datetime
import tempfile
import traceback
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")
TIMEOUT = 60
UPLOAD_TIMEOUT = 60 * 20


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


class Ctx:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.tz = ZoneInfo(cfg.get("timezone") or "Asia/Karachi")
        self.now = datetime.datetime.now(self.tz)
        self.tokens = {}
        self.scanned = set()

    def ts(self):
        return datetime.datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")

    def log(self, msg, level="info", acc=None):
        print(f"[{self.ts()}] {level.upper()} {('@' + acc['username'] + ': ') if acc else ''}{msg}", flush=True)
        self.state.setdefault("log", []).insert(0, {
            "t": self.ts(), "l": level, "a": acc["id"] if acc else "", "m": msg[:400]})
        del self.state["log"][300:]

    def acc_state(self, acc):
        st = self.state.setdefault("accounts", {}).setdefault(acc["id"], {})
        st.setdefault("videos", [])
        st.setdefault("seen", [])
        st.setdefault("slots", {})
        st.setdefault("last_checked", None)
        st.setdefault("first_done", False)
        return st


def decrypt_tokens(enc, password):
    """iPhone app Page tokens ko password se lock (AES-GCM) kar ke config.json mein rakhti hai."""
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


def build_caption(original, acc):
    text = original if acc.get("use_caption", True) else ""
    if text and acc.get("remove_hashtags"):
        text = re.sub(r"(?<!\w)#\S+", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    extra = (acc.get("extra") or "").strip()
    if extra:
        text = f"{text}\n\n{extra}" if text else extra
    return text.strip()


# ------------------------------------------------------------------ TikTok
def _ydl(opts):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    cookies = os.path.join(ROOT, "cookies.txt")
    if os.path.isfile(cookies):
        base["cookiefile"] = cookies
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def tiktok_list(url, limit):
    with _ydl({"extract_flat": "in_playlist", "skip_download": True, "playlistend": int(limit)}) as y:
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
        out.append({"id": vid, "url": vurl, "caption": e.get("description") or e.get("title") or ""})
    return out


def tiktok_download(url, folder):
    with _ydl({"outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
               "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best",
               "noplaylist": True, "retries": 5}) as y:
        info = y.extract_info(url, download=True)
        req = info.get("requested_downloads") or []
        path = (req[0].get("filepath") if req else None) or y.prepare_filename(info)
    if not path or not os.path.isfile(path):
        raise RuntimeError("File not found after download")
    return path, info.get("description") or info.get("title") or ""


# ------------------------------------------------------------------ Facebook
class FBError(Exception):
    pass


def _fb(resp):
    try:
        data = resp.json()
    except ValueError:
        raise FBError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if isinstance(data, dict) and data.get("error"):
        e = data["error"]
        raise FBError(f"{e.get('message')} (code {e.get('code')})")
    if resp.status_code >= 400:
        raise FBError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return data


def fb_upload_reel(ver, page_id, token, path, desc):
    g = f"https://graph.facebook.com/{ver}"
    start = _fb(requests.post(f"{g}/{page_id}/video_reels",
                              data={"upload_phase": "start", "access_token": token}, timeout=TIMEOUT))
    vid = start["video_id"]
    up_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{ver}/{vid}"
    with open(path, "rb") as f:
        _fb(requests.post(up_url, headers={"Authorization": f"OAuth {token}", "offset": "0",
                                           "file_size": str(os.path.getsize(path))},
                          data=f, timeout=UPLOAD_TIMEOUT))
    _fb(requests.post(f"{g}/{page_id}/video_reels", data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": desc, "access_token": token}, timeout=TIMEOUT))
    return vid


def fb_upload_video(ver, page_id, token, path, desc):
    with open(path, "rb") as f:
        data = _fb(requests.post(f"https://graph-video.facebook.com/{ver}/{page_id}/videos",
                                 data={"description": desc, "access_token": token},
                                 files={"source": (os.path.basename(path), f, "video/mp4")},
                                 timeout=UPLOAD_TIMEOUT))
    return data.get("id") or data.get("post_id")


def fb_upload(ver, page_id, token, path, desc, mode):
    if mode == "video":
        return fb_upload_video(ver, page_id, token, path, desc), "video"
    try:
        return fb_upload_reel(ver, page_id, token, path, desc), "reel"
    except FBError:
        return fb_upload_video(ver, page_id, token, path, desc), "video (reel failed)"


# ------------------------------------------------------------------ core
def scan(ctx, acc):
    st = ctx.acc_state(acc)
    ctx.scanned.add(acc["id"])
    first = not st["first_done"]
    limit = int(ctx.cfg.get("scan_limit") or 30)
    if first and acc.get("import_old", True):
        limit = max(limit, 200)
    try:
        items = tiktok_list(normalize_url(acc["tiktok"]), limit)
    except Exception as e:
        ctx.log(f"TikTok check failed - {e}", "error", acc)
        st["last_checked"] = ctx.ts()
        return 0
    seen = set(st["seen"])
    new = 0
    for it in reversed(items):  # purani pehle
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        st["seen"].append(it["id"])
        status = "skipped" if (first and not acc.get("import_old", True)) else "new"
        st["videos"].append({"id": it["id"], "url": it["url"], "caption": it["caption"][:300],
                             "status": status, "attempts": 0, "found": ctx.ts()})
        new += status == "new"
    st["first_done"] = True
    st["last_checked"] = ctx.ts()
    if first and not acc.get("import_old", True):
        ctx.log(f"first check: skipped {len(items)} old videos, only new ones will be uploaded", "info", acc)
    else:
        ctx.log(f"check done: {new} new videos added to queue", "info", acc)
    return new


def process(ctx, acc, video):
    page_id = str(acc.get("page_id") or "")
    token = ctx.tokens.get(page_id)
    if not token:
        ctx.log("no token for this account's Page. In the app go to Pages and connect Facebook.", "error", acc)
        return False
    max_attempts = int(ctx.cfg.get("max_attempts") or 3)
    ver = ctx.cfg.get("graph_version") or "v23.0"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path, cap = tiktok_download(video["url"], tmp)
            cap = cap or video.get("caption") or ""
            video["caption"] = cap[:300]
            post_id, kind = fb_upload(ver, page_id, token, path, build_caption(cap, acc), acc.get("mode", "reel"))
            video.update(status="uploaded", fb=str(post_id), uploaded=ctx.ts(), error="")
            page_name = next((p["name"] for p in ctx.cfg.get("pages", []) if str(p["id"]) == page_id), page_id)
            ctx.log(f"video {video['id'][-6:]} -> {page_name} uploaded ({kind})", "success", acc)
            return True
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
        tries += 1
        video = next((v for v in st["videos"] if v["status"] in ("new", "retry")), None)
        if not video:
            if acc["id"] not in ctx.scanned and scan(ctx, acc) > 0:
                continue
            ctx.log("no new videos in queue", "info", acc)
            break
        if process(ctx, acc, video):
            done += 1
    return done


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
    done = st["slots"].setdefault(today, [])
    new_account = st["last_checked"] is None

    # TikTok check (har ghante, ya settings ke mutabiq)
    every = int(ctx.cfg.get("check_minutes") or 60)
    due = True
    if st["last_checked"]:
        try:
            last = datetime.datetime.strptime(st["last_checked"], "%Y-%m-%d %H:%M").replace(tzinfo=ctx.tz)
            due = (ctx.now - last).total_seconds() >= (every - 5) * 60
        except ValueError:
            pass
    if due:
        scan(ctx, acc)

    passed = [s for s in slots if s <= hhmm]
    if new_account:  # naya account: aaj ke guzre slots par ek dum upload na karo
        done.extend(s for s in passed if s not in done)
        return
    pending = [s for s in passed if s not in done]
    if pending:
        done.extend(pending)
        ctx.log(f"{pending[-1]} slot - starting upload", "info", acc)
        post_next(ctx, acc, max(1, int(acc.get("per_slot") or 1)))


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

    if cfg.get("tokens_enc"):
        try:
            ctx.tokens = {str(k): v for k, v in decrypt_tokens(cfg["tokens_enc"], os.environ.get("APP_KEY", "")).items()}
        except Exception:
            ctx.log("Could not unlock Facebook tokens: GitHub Secret 'APP_KEY' must match the app password", "error")
    else:
        ctx.log("Facebook is not connected yet (see the Pages tab in the app)", "error")

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
                elif action == "add_video":
                    m = re.search(r"/video/(\d+)", video_arg)
                    if not m:
                        ctx.log("invalid TikTok video link", "error", acc)
                    elif m.group(1) in st["seen"]:
                        ctx.log("this video is already in the list", "info", acc)
                    else:
                        st["seen"].append(m.group(1))
                        st["videos"].append({"id": m.group(1), "url": video_arg.split("?")[0], "caption": "",
                                             "status": "new", "attempts": 0, "found": ctx.ts()})
                        ctx.log("video added to queue", "info", acc)
    except Exception as e:
        ctx.log(f"Engine error: {e} | {traceback.format_exc()[-300:]}", "error")

    # purane / delete kiye gaye accounts ka data saaf
    ids = {a["id"] for a in accounts}
    for aid in list(state.get("accounts", {})):
        if aid not in ids:
            del state["accounts"][aid]
    for st in state.get("accounts", {}).values():
        trim(st)
    state["last_run"] = ctx.ts()
    state["last_action"] = action
    save_state(state)


if __name__ == "__main__":
    main()
