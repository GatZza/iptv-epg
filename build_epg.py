#!/usr/bin/env python3
"""
Build a supplementary XMLTV EPG for the "US Movies" channel category of a
personal IPTV playlist, using real programme listings pulled from the
free public epgshare01.online guide (Gracenote/tmsapi-sourced data).

Why this exists
----------------
The playlist's own EPG source (epg.iptv.cat) doesn't cover the "US Movies"
category. Most of those channels are real, named premium US networks
(HBO, Showtime, Starz, Cinemax, MGM+, TCM, Sundance, FXM, Sony Movies,
HDNet Movies, Lifetime Movie Network, REELZ, ScreenPix, IndiePlex,
MoviePlex, RetroPlex, Hallmark, El Rey, FilmRise, etc.) that DO have
real published schedules. A handful of entries in that category
("CINEBOX: ACTION", "CINEBOX: COMEDY", ...) are generic genre movie
wheels with no tvg-id and no real-world schedule anywhere -- those are
intentionally skipped.

What it does
------------
1. Downloads your M3U playlist and pulls out every channel whose
   group-title is exactly "US Movies".
2. Downloads epgshare01's free US ("US2") XMLTV guide index + data.
3. Matches each playlist channel to the right source channel by
   normalizing names (stripping HD/SD/East/West/quality-code noise) and
   a curated alias table for renamed/rebranded channels (e.g. EPIX -> MGM+,
   TCM -> Turner Classic Movies, ActionMax -> Cinemax Action, Showtime ->
   Paramount+ with Showtime, etc.), preferring the East feed unless the
   playlist entry says West/Pacific.
4. Emits a fresh XMLTV file where each <channel id="..."> matches the
   tvg-id already used in your playlist (so players that match EPG by
   tvg-id, like TiviMate, pick it up automatically with zero playlist
   changes), populated with several days of real programmes copied from
   the matched source channel.
5. Writes a match report so it's obvious what got covered and what didn't.

This script has NO third-party dependencies -- stdlib only -- so it needs
nothing installed beyond Python 3.9+.
"""

import gzip
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

M3U_URL = os.environ.get("M3U_URL", "").strip()
GROUP_TITLE = os.environ.get("EPG_GROUP_TITLE", "US Movies").strip()
SOURCE_INDEX_URL = "https://epgshare01.online/epgshare01/epg_ripper_US2.txt"
SOURCE_XML_URL = "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz"
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "public")
USER_AGENT = "Mozilla/5.0 (compatible; personal-epg-builder/1.0)"

SKIP_PREFIXES = ("CINEBOX",)  # generic genre movie wheels: no real schedule anywhere

# Normalized-name -> normalized-source-name overrides for channels that are
# renamed/rebranded/abbreviated between the playlist and the source guide.
# A value of None means "known to have no real source available; skip
# quietly instead of reporting it as a mystery every run".
ALIASES = {
    "epix": "mgm",
    "epixeast": "mgm",
    "epixhits": "mgmhits",
    "epixdrivein": "mgmdrivein",
    "lifetimemovie": "lmn",
    "lifetimemovielmn": "lmn",
    "fxm": "fxmovie",
    "actionmax": "cinemaxaction",
    "moremax": "cinemaxhits",
    "5starmax": "cinemaxclassics",
    "reelz": "reelzchannel",
    "hdnetmovies": "hdnetmovies",
    "elrey": "elrey",
    "elreyfhd": "elrey",
    "sonymovies": "sonymovie",
    "sonymoviescannel": "sonymovie",
    "themovietmc": "themovie",
    "themovieextratmc": "themovieextra",
    "sundance": "sundancetv",
    "oan": "oneamericanews",
    "oneamericanewsoan": "oneamericanews",
    "africachannel": "theafrica",
    "fusemusicfm": "fuse",
    "fusemusic": "fuse",
    "hallmarkchannelwestsp": "hallmarkchannel",
    "starzkidsfamily": "starzkids",
    "starzinblack": "starzinblack",
    "moviemax": None,
    "showtime": "paramountwithshowtime",
    "hbofamily": None,
    "gacfamily": None,
    "gacliving": None,
    "dust": None,
    "afro": None,
    "filmrisefreemovies": None,
    "filmrisewestern": None,
    "filmrisetruecrime": None,
    "filmriseclassictv": None,
    "filmriseanime": None,
    "shortstv": "shortstv",
    "screenpixvoicesus": "screenpixvoices",
    "screenpixactionus": "screenpixaction",
    "screenpixwesternsus": "screenpixwesterns",
    "screenpixus": "screenpix",
    "showtimebet": "shoxbet",
    "tcm": "turnerclassicmovies",
    "thrillermax": None,
    "outermax": None,
    "cinemaxlatino": "cinemaxspanish",
    "starzencorewestern": "starzencorewesterns",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(name):
    n = strip_accents(name)
    n = re.sub(r"^\(?US\)?:?\s*", "", n, flags=re.I)
    n = re.sub(r"^\(SP\)\s*", "", n, flags=re.I)
    n = re.sub(r"\.us2$", "", n)
    n = n.replace(".", " ").replace("-", " ")
    n = re.sub(r"\([^)]*\)", "", n)  # strip all parenthetical qualifiers
    n = re.sub(r"\b(HD|SD|FHD|East|West|Pacific|Channel|Network|Feed|US)\b", "", n, flags=re.I)
    n = re.sub(r"[^a-z0-9]", "", n.lower())
    return n


def region_hint(name):
    nl = name.lower()
    return "west" if ("pacific" in nl or "west" in nl) else "east"


def slugify(name):
    n = normalize(name)
    return f"usmovies-{n}" if n else "usmovies-unknown"


# --------------------------------------------------------------------------
# Step 1: parse the playlist
# --------------------------------------------------------------------------

EXTINF_RE = re.compile(r'group-title="([^"]*)"')
TVGID_RE = re.compile(r'tvg-id="([^"]*)"')


def parse_m3u_group(text, group_title):
    lines = text.split("\n")
    entries = []
    seen = set()
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        gm = EXTINF_RE.search(line)
        if not gm or gm.group(1) != group_title:
            continue
        idm = TVGID_RE.search(line)
        tvg_id = idm.group(1) if idm else ""
        display = line.split(",", 1)[-1].strip()
        url = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if display.upper().startswith(SKIP_PREFIXES):
            continue
        key = (tvg_id, display)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"tvg_id": tvg_id, "display": display, "url": url})
    return entries


# --------------------------------------------------------------------------
# Step 2: build the source channel index + match table
# --------------------------------------------------------------------------

def build_source_index(index_text):
    ids = [
        l.strip()
        for l in index_text.splitlines()
        if l.strip() and not l.startswith("--") and not l.strip().isdigit()
    ]
    by_norm = {}
    for cid in ids:
        n = normalize(cid)
        r = region_hint(cid)
        by_norm.setdefault(n, []).append((cid, r))
    return by_norm


def match_channel(display, source_by_norm):
    norm = normalize(display)
    hint = region_hint(display)
    target = ALIASES.get(norm, norm)
    if target is None:
        return None
    candidates = source_by_norm.get(target)
    if not candidates:
        return None
    for cid, r in candidates:
        if r == hint:
            return cid
    return candidates[0][0]


# --------------------------------------------------------------------------
# Step 3: stream the big XML and pull only the programmes we need
# --------------------------------------------------------------------------

def extract_programmes(xml_gz_bytes, wanted_source_ids):
    """Stream-parse the (large) source XMLTV and return
    {source_channel_id: [<programme> Element, ...]} for only the channel ids
    we actually need, without holding the whole 70+MB document in memory
    as a parsed tree."""
    result = {cid: [] for cid in wanted_source_ids}
    raw = gzip.decompress(xml_gz_bytes)
    stream = io.BytesIO(raw)
    context = ET.iterparse(stream, events=("end",))
    for event, elem in context:
        if elem.tag == "programme":
            ch = elem.get("channel")
            if ch in result:
                result[ch].append(elem)
            else:
                elem.clear()
        elif elem.tag == "channel":
            elem.clear()
    return result


# --------------------------------------------------------------------------
# Step 4: emit our output XMLTV
# --------------------------------------------------------------------------

def build_output(entries_with_match, programmes_by_source):
    tv = ET.Element("tv", {
        "generator-info-name": "personal-epg-builder",
        "generator-info-url": "https://github.com/",
    })

    # one <channel> per distinct output id
    seen_ids = set()
    for e in entries_with_match:
        if e["output_id"] in seen_ids:
            continue
        seen_ids.add(e["output_id"])
        ch = ET.SubElement(tv, "channel", {"id": e["output_id"]})
        dn = ET.SubElement(ch, "display-name")
        dn.text = e["display"]

    # programmes: copy from the matched source, retagged to our output id
    for e in entries_with_match:
        src_id = e["source_id"]
        progs = programmes_by_source.get(src_id, [])
        for p in progs:
            new_p = ET.fromstring(ET.tostring(p))
            new_p.set("channel", e["output_id"])
            tv.append(new_p)

    return tv


def write_gzip(path, data_bytes):
    with gzip.open(path, "wb") as f:
        f.write(data_bytes)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    if not M3U_URL:
        log("ERROR: M3U_URL environment variable is not set.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    log(f"Fetching playlist ...")
    m3u_text = fetch(M3U_URL)
    entries = parse_m3u_group(m3u_text, GROUP_TITLE)
    log(f"Found {len(entries)} entries in group {GROUP_TITLE!r} (CINEBOX-style entries already excluded).")

    log("Fetching source channel index ...")
    index_text = fetch(SOURCE_INDEX_URL)
    source_by_norm = build_source_index(index_text)

    matched, unmatched = [], []
    for e in entries:
        src = match_channel(e["display"], source_by_norm)
        output_id = e["tvg_id"] if e["tvg_id"] else slugify(e["display"])
        if src:
            matched.append({**e, "source_id": src, "output_id": output_id})
        else:
            unmatched.append(e)

    distinct_sources = sorted(set(m["source_id"] for m in matched))
    log(f"Matched {len(matched)} entries to {len(distinct_sources)} distinct real source channels.")
    log(f"{len(unmatched)} entries have no available real-world schedule and were skipped.")

    log("Downloading + streaming the source guide (this is the big one, ~6-7MB compressed) ...")
    xml_gz_bytes = fetch(SOURCE_XML_URL, binary=True)
    programmes_by_source = extract_programmes(xml_gz_bytes, set(distinct_sources))
    total_progs = sum(len(v) for v in programmes_by_source.values())
    log(f"Pulled {total_progs} programme entries for matched channels.")

    tv = build_output(matched, programmes_by_source)
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")

    out_xml_path = os.path.join(OUTPUT_DIR, "epg.xml")
    out_gz_path = os.path.join(OUTPUT_DIR, "epg.xml.gz")
    with open(out_xml_path, "wb") as f:
        f.write(xml_bytes)
    write_gzip(out_gz_path, xml_bytes)
    log(f"Wrote {out_xml_path} ({len(xml_bytes)} bytes) and {out_gz_path}.")

    # --- match report ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_lines = [
        f"# US Movies EPG build report",
        f"",
        f"Last built: {now}",
        f"",
        f"- Playlist entries in \"{GROUP_TITLE}\": {len(entries)} (after de-duplicating and dropping CINEBOX-style generic channels)",
        f"- Matched to a real published schedule: {len(matched)}",
        f"- Distinct real source channels used: {len(distinct_sources)}",
        f"- Programme entries written: {total_progs}",
        f"- Unmatched (no real-world schedule available, skipped): {len(unmatched)}",
        f"",
        f"## Unmatched channels",
        f"",
    ]
    if unmatched:
        for e in unmatched:
            tag = e["tvg_id"] or "(no tvg-id)"
            report_lines.append(f"- `{tag}` - {e['display']}")
    else:
        report_lines.append("_None - everything matched!_")
    report_lines += [
        "",
        "## Channels without a tvg-id in the playlist",
        "",
        "These were still included (under a generated id), but most players "
        "won't auto-match them to EPG data without a tvg-id in the playlist "
        "itself. You may need to map them manually in your player's EPG "
        "settings (search by channel name).",
        "",
    ]
    no_id = [e for e in matched if not e["tvg_id"]]
    if no_id:
        for e in no_id:
            report_lines.append(f"- `{e['output_id']}` - {e['display']}")
    else:
        report_lines.append("_None - every matched channel already had a tvg-id._")

    with open(os.path.join(OUTPUT_DIR, "match-report.md"), "w") as f:
        f.write("\n".join(report_lines) + "\n")

    # --- tiny status page ---
    index_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>US Movies EPG</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5}}
code{{background:#f0f0f0;padding:2px 6px;border-radius:4px}}</style></head>
<body>
<h1>US Movies EPG</h1>
<p>Last built: {now}</p>
<p><a href="epg.xml">epg.xml</a> &middot; <a href="epg.xml.gz">epg.xml.gz</a> (gzip, smaller/faster to load)</p>
<p>{len(matched)} of {len(entries)} channels matched to real schedules
({len(distinct_sources)} distinct source channels, {total_progs} programme entries).</p>
<p>See <a href="match-report.md">match-report.md</a> for exactly which channels matched and which didn't.</p>
</body></html>
"""
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w") as f:
        f.write(index_html)

    log("Done.")


if __name__ == "__main__":
    main()
