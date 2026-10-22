#!/usr/bin/env python3
"""
iptv_repair.py - build a self-repairing world IPTV playlist from the public iptv-org/iptv lists.

What it does on every run:
  1. (--update) downloads the latest iptv-org/iptv stream lists, so brand-new links are picked up.
  2. Health-checks every link (HLS playlist -> variant playlist -> first video segment).
  3. For each channel keeps ONE working link. If the current link has failed --fail-limit runs
     in a row, it is swapped for the best working alternative for the same channel.
     A channel with no working link is kept for --grace-days on its last good link, then dropped.
     It comes back by itself when any of its links works again.
  4. Writes out/all.m3u, out/by-country/<cc>.m3u, out/state.json and out/report.txt.

Standard library only (Python 3.8+). Run it from the country you watch in: some streams are
geo-blocked and look dead from abroad.
"""
import argparse, concurrent.futures as cf, datetime as dt, io, json, os, re, sys, time
import urllib.request, urllib.error, urllib.parse, zipfile

UPSTREAM_ZIP = "https://github.com/iptv-org/iptv/archive/refs/heads/master.zip"
COUNTRIES_URL = "https://iptv-org.github.io/api/countries.json"
DEFAULT_UA = "Mozilla/5.0 (Linux; Android 9; TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
RES_RE = re.compile(r"\((\d{3,4})p\)")


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


# ----------------------------------------------------------------------------- upstream
def download_upstream(dest):
    """Download the iptv-org/iptv zip and extract only streams/*.m3u into dest/streams."""
    print("Downloading latest iptv-org lists ...")
    req = urllib.request.Request(UPSTREAM_ZIP, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    out = os.path.join(dest, "streams")
    os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.endswith(".m3u"):
            os.remove(os.path.join(out, f))
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            parts = name.split("/")
            if len(parts) == 3 and parts[1] == "streams" and parts[2].endswith(".m3u"):
                with z.open(name) as src, open(os.path.join(out, parts[2]), "wb") as dst:
                    dst.write(src.read())
                n += 1
    if n == 0:
        raise RuntimeError("upstream zip had no streams/*.m3u; layout may have changed")
    print(f"  {n} list files")


def load_countries(cache_path):
    """code -> name. Network first, then cache, then empty (group names fall back to codes)."""
    try:
        req = urllib.request.Request(COUNTRIES_URL, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        m = {c["code"].lower(): c["name"] for c in data if "code" in c and "name" in c}
        if m:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(m, f)
            return m
    except Exception:
        pass
    try:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_m3u(path, stem):
    """Yield dicts: key, title, attrs, opts, url, src (file stem), height."""
    entries, cur, opts = [], None, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip().lstrip("﻿")
            if not line:
                continue
            if line.startswith("#EXTINF"):
                cur = line
                opts = []
            elif line.startswith("#EXTVLCOPT"):
                opts.append(line)
            elif line.startswith("#"):
                continue
            elif cur is not None:
                head, _, title = cur.rpartition(",")
                attrs = dict(ATTR_RE.findall(head))
                title = title.strip()
                tvg = attrs.get("tvg-id", "").strip()
                norm = re.sub(r"\W+", "", RES_RE.sub("", title).lower())
                key = tvg if tvg else "name:" + stem.split("_")[0] + ":" + norm
                m = RES_RE.search(title)
                entries.append({"key": key, "title": title, "attrs": attrs, "opts": opts[:],
                                "url": line, "src": stem, "height": int(m.group(1)) if m else 0})
                cur, opts = None, []
    return entries


def load_upstream(streams_dir, only_countries):
    entries = []
    for fn in sorted(os.listdir(streams_dir)):
        if not fn.endswith(".m3u"):
            continue
        stem = fn[:-4]
        if only_countries and stem.split("_")[0] not in only_countries:
            continue
        entries += parse_m3u(os.path.join(streams_dir, fn), stem)
    return entries


# ----------------------------------------------------------------------------- health check
def headers_for(entry):
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    for o in entry["opts"]:
        k, _, v = o.partition("=")
        if k.endswith("http-user-agent"):
            h["User-Agent"] = v
        elif k.endswith("http-referrer"):
            h["Referer"] = v
    return h


def fetch(url, hdr, timeout, limit, byte_range=None):
    h = dict(hdr)
    if byte_range:
        h["Range"] = byte_range
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, (r.headers.get("Content-Type") or "").lower(), r.read(limit), r.geturl()


def check_hls(base, data, hdr, timeout, deep, depth=0):
    text = data.decode("utf-8", errors="replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if depth > 3:
        return False, "playlist nesting too deep"
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        for i, l in enumerate(lines):                       # first variant
            if l.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[i + 1:]:
                    if not nxt.startswith("#"):
                        if not deep:
                            return True, "ok (master playlist)"
                        st, _, d, final = fetch(urllib.parse.urljoin(base, nxt), hdr, timeout, 65536)
                        return check_hls(final, d, hdr, timeout, deep, depth + 1)
                break
        return False, "master playlist without variants"
    segs = [l for l in lines if not l.startswith("#")]
    if not segs:
        return False, "empty playlist"
    if not deep:
        return True, "ok (media playlist)"
    seg = urllib.parse.urljoin(base, segs[0])
    if seg.split("?")[0].endswith(".m3u8"):
        st, _, d, final = fetch(seg, hdr, timeout, 65536)
        return check_hls(final, d, hdr, timeout, deep, depth + 1)
    st, _, d, _ = fetch(seg, hdr, timeout, 2048, "bytes=0-2047")
    return (st in (200, 206) and len(d) > 0), f"segment http {st}, {len(d)} bytes"


def check_stream(entry, timeout, deep):
    hdr = headers_for(entry)
    try:
        st, ctype, data, final = fetch(entry["url"], hdr, timeout, 65536)
        if st != 200:
            return False, f"http {st}"
        head = data.lstrip(b"\xef\xbb\xbf \r\n\t")
        if head.startswith(b"#EXTM3U"):
            return check_hls(final, data, hdr, timeout, deep)
        if b"<MPD" in data[:4000]:
            return True, "ok (dash)"
        if (ctype.startswith(("video/", "audio/")) or "mpegurl" in ctype or "octet-stream" in ctype) and len(data) >= 188:
            return True, "ok (direct stream)"
        return False, f"not a stream ({ctype or 'no content-type'})"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except Exception as e:
        return False, type(e).__name__


def check_all(entries, workers, timeout, deep):
    urls = {}
    for e in entries:
        urls.setdefault(e["url"], e)                         # one check per unique URL
    results, done, total, t0 = {}, 0, len(urls), time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_stream, e, timeout, deep): u for u, e in urls.items()}
        for fu in cf.as_completed(futs):
            results[futs[fu]] = fu.result()
            done += 1
            if done % 500 == 0 or done == total:
                ok = sum(1 for v in results.values() if v[0])
                print(f"  checked {done}/{total}  working {ok}  ({int(time.time() - t0)}s)")
    return results


# ----------------------------------------------------------------------------- selection
def score(entry, max_height):
    h = entry["height"]
    return (min(h, max_height) - (0.5 if h > max_height else 0)) if h else 1


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("urls", {}); s.setdefault("channels", {})
        return s
    except Exception:
        return {"urls": {}, "channels": {}}


def run(args):
    out = args.out
    os.makedirs(os.path.join(out, "by-country"), exist_ok=True)
    state_path = os.path.join(out, "state.json")
    state = load_state(state_path)
    t = now()

    if args.update:
        download_upstream(args.work)
    streams_dir = os.path.join(args.work, "streams")
    if not os.path.isdir(streams_dir):
        sys.exit(f"No lists in {streams_dir}. Run once with --update.")
    only = {c.strip().lower() for c in args.countries.split(",")} if args.countries else None
    entries = load_upstream(streams_dir, only)
    print(f"{len(entries)} links, {len({e['key'] for e in entries})} channels in the upstream lists")

    results = check_all(entries, args.workers, args.timeout, not args.shallow)

    # per-URL history
    for url, (ok, why) in results.items():
        u = state["urls"].setdefault(url, {"fails": 0, "last_ok": None})
        if ok:
            u["fails"], u["last_ok"] = 0, iso(t)
        else:
            u["fails"] += 1
        u["last_checked"], u["why"] = iso(t), why
    live_urls = set(results)
    state["urls"] = {u: v for u, v in state["urls"].items() if u in live_urls}   # forget removed links

    def healthy(url):
        s = state["urls"][url]
        return s["fails"] < args.fail_limit and s["last_ok"] is not None

    chans = {}
    for e in entries:
        chans.setdefault(e["key"], []).append(e)

    chosen, repairs, dropped, kept_grace = [], [], [], []
    new_channels_state = {}
    for key, cands in chans.items():
        prev = state["channels"].get(key, {})
        by_url = {c["url"]: c for c in cands}
        pick = None
        if prev.get("url") in by_url and healthy(prev["url"]):
            pick = by_url[prev["url"]]                        # stability: keep a link that still works
        else:
            good = [c for c in cands if results[c["url"]][0]]            # works right now
            if not good:
                good = [c for c in cands if healthy(c["url"])]           # recently ok, within fail limit
            if good:
                pick = max(good, key=lambda c: score(c, args.max_height))
                if prev.get("url") and prev["url"] != pick["url"]:
                    repairs.append((key, prev["url"], pick["url"], pick["title"]))
        if pick is None:
            # nothing works: hold the last good link for a grace period, then drop
            pu = prev.get("url")
            if pu in by_url and prev.get("last_ok") and (t - parse_iso(prev["last_ok"])).days < args.grace_days:
                pick = by_url[pu]
                kept_grace.append((key, pick["title"]))
            else:
                dropped.append((key, cands[0]["title"]))
                continue
        s = state["urls"][pick["url"]]
        new_channels_state[key] = {"url": pick["url"], "last_ok": s["last_ok"] or prev.get("last_ok")}
        chosen.append(pick)
    state["channels"] = new_channels_state
    state["updated"] = iso(t)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)

    # ------------------------------------------------------------------ write playlists
    names = load_countries(os.path.join(args.work, "countries.json"))

    def country(e):
        """Country from the channel's own id (Name.cc@feed); else from the list file it came from."""
        m = re.search(r"\.([a-z]{2})(?:@|$)", e["attrs"].get("tvg-id", ""))
        return m.group(1) if m else e["src"].split("_")[0]

    def group(e):
        cc = country(e)
        return names.get(cc, cc.upper())

    def block(e):
        attrs = {k: v for k, v in e["attrs"].items() if k != "group-title"}
        attrs["group-title"] = group(e)
        a = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        return "\n".join([f"#EXTINF:-1 {a},{e['title']}"] + e["opts"] + [e["url"]]) + "\n"

    chosen.sort(key=lambda e: (group(e).lower(), e["title"].lower()))
    with open(os.path.join(out, "all.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + "".join(block(e) for e in chosen))
    by_cc = {}
    for e in chosen:
        by_cc.setdefault(country(e), []).append(e)
    for cc, lst in by_cc.items():
        with open(os.path.join(out, "by-country", f"{cc}.m3u"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n" + "".join(block(e) for e in lst))

    ok_n = sum(1 for v in results.values() if v[0])
    rep = [f"IPTV repair run {iso(t)}",
           f"links checked: {len(results)}   working now: {ok_n}",
           f"channels in playlist: {len(chosen)}   (upstream channels: {len(chans)})",
           f"repaired (swapped to a new link): {len(repairs)}",
           f"held on last good link (grace): {len(kept_grace)}",
           f"dropped (no working link): {len(dropped)}", ""]
    rep += [f"REPAIRED {k}: {n}\n    old {o}\n    new {nw}" for k, o, nw, n in repairs]
    rep += [f"GRACE    {k}: {n}" for k, n in kept_grace]
    rep += [f"DROPPED  {k}: {n}" for k, n in dropped]
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(rep) + "\n")
    with open(os.path.join(out, "repairs.log"), "a", encoding="utf-8") as f:
        for k, o, nw, n in repairs:
            f.write(f"{iso(t)}\t{k}\t{n}\t{o}\t->\t{nw}\n")
    print("\n".join(rep[:6]))
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--update", action="store_true", help="download the latest iptv-org lists first")
    p.add_argument("--work", default="upstream", help="folder holding streams/*.m3u (default: upstream)")
    p.add_argument("--out", default="out", help="output folder (default: out)")
    p.add_argument("--countries", default="", help="only these country codes, e.g. in,us,gb (default: all)")
    p.add_argument("--workers", type=int, default=48, help="parallel checks (default 48)")
    p.add_argument("--timeout", type=float, default=8.0, help="seconds per request (default 8)")
    p.add_argument("--shallow", action="store_true", help="only fetch the playlist, skip the video segment test (faster, less accurate)")
    p.add_argument("--fail-limit", type=int, default=2, help="consecutive failed runs before a link is replaced (default 2)")
    p.add_argument("--grace-days", type=int, default=7, help="days to keep a channel whose links all fail (default 7)")
    p.add_argument("--max-height", type=int, default=1080, help="prefer links up to this resolution (default 1080)")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
