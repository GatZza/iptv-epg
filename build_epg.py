#!/usr/bin/env python3
"""
Build a supplementary XMLTV EPG for several channel categories of a personal
IPTV playlist, using real programme listings pulled from the free public
epgshare01.online guide (Gracenote/tmsapi-sourced data).

Why this exists
----------------
The playlist's own EPG source (epg.iptv.cat) doesn't cover these
categories. Most of the channels in them are real, named networks that DO
have real published schedules. A handful of entries are generic genre
wheels with no tvg-id and no real-world schedule anywhere ("CINEBOX: ...")
-- those are intentionally skipped.

Categories covered
-------------------
- US Movies         (epgshare01 tag US2)
- US Documentaries   (epgshare01 tag US2)
- UK Sports          (epgshare01 tag UK1)

What it does
------------
1. Downloads your M3U playlist and, for each configured category, pulls out
   every channel whose group-title matches exactly.
2. Downloads epgshare01's free XMLTV guide index + data for each source tag
   needed (each tag is only fetched once even if several categories share
   it, e.g. US2 for both US Movies and US Documentaries).
3. Matches each playlist channel to the right source channel by
   normalizing names (stripping HD/SD/East/West/quality-code noise) and a
   curated alias table per source tag for renamed/rebranded channels
   (e.g. EPIX -> MGM+, TCM -> Turner Classic Movies, Sky Sports Racing ->
   SkySp.Racing, etc.), preferring the East feed unless the playlist entry
   says West/Pacific.
4. Emits a fresh XMLTV file where each <channel id="..."> matches the
   tvg-id already used in your playlist (so players that match EPG by
   tvg-id, like TiviMate, pick it up automatically with zero playlist
   changes), populated with several days of real programmes copied from
   the matched source channel.
5. Writes a match report, broken out per category, so it's obvious what
   got covered and what didn't.

This script has NO third-party dependencies -- stdlib only -- so it needs
nothing installed beyond Python 3.9+.
"""

import gzip
import io
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
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "public")
USER_AGENT = "Mozilla/5.0 (compatible; personal-epg-builder/1.0)"

SKIP_PREFIXES = ("CINEBOX",)  # generic genre wheels: no real schedule anywhere

# Free public XMLTV source, per epgshare01 region/category "tag".
SOURCE_TAGS = {
    "US2": {
        "index_url": "https://epgshare01.online/epgshare01/epg_ripper_US2.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    },
    "UK1": {
        "index_url": "https://epgshare01.online/epgshare01/epg_ripper_UK1.txt",
        "xml_url": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    },
}

# Each playlist group-title we cover, and which source tag its real-world
# schedules come from. "slug" is used to build a generated output id for
# channels that have no tvg-id in the playlist.
TARGETS = [
    {"group_title": "US Movies", "tag": "US2", "slug": "usmovies", "label": "US Movies"},
    {"group_title": "US Documentaries", "tag": "US2", "slug": "usdocs", "label": "US Documentaries"},
    {"group_title": "UK Sports", "tag": "UK1", "slug": "uksports", "label": "UK Sports"},
]

# Normalized-playlist-name -> normalized-source-name overrides, per source
# tag, for channels that are renamed/rebranded/abbreviated between the
# playlist and the source guide. A value of None means "known to have no
# real source available; skip quietly instead of reporting it as a mystery
# every run".
ALIASES = {
    "US2": {
        # --- US Movies ---
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
        "gac": None,
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
        # --- US Documentaries ---
        "smithsonian": "smithsoniannetwork",
        "idinvestigationdiscovery": "investigationdiscovery",
        "natgeo": "nationalgeographic",
        "natgeowild": "nationalgeographicwild",
        "travelchannel": "thetravelchannel",
        "discoverylife": "discoverylifechannel",
        "fyi": "fyichannel",
        "discoveryfamily": "discoveryfamilychannel",
        "lawandcrimenetwork": "lawandcrime",
        "hgtv": None,
        "ae": None,
        "discoveryscience": None,
        "greatamericancountry": None,
    },
    "UK1": {
        # --- UK Sports ---
        "skysportsracing": "skyspracing",
        "premiersports1": "premiersport1",
        "premiersports2": "premiersport2",
        "skysportsmainevent": None,
        "skysportsf1": None,
        "skysportspremierleague": None,
        "skysportsgolf": None,
        "skysportsnews": None,
        "skysportscricket": None,
        "skysportsarena": None,
        "skysportsmix": None,
    },
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
    n = re.sub(r"^\(?(US|UK)\)?:?\s*", "", n, flags=re.I)
    n = re.sub(r"^\(SP\)\s*", "", n, flags=re.I)
    n = re.sub(r"\.(us2|uk)$", "", n, flags=re.I)
    n = n.replace("&", " and ")
    n = n.replace(".", " ").replace("-", " ")
    n = re.sub(r"\([^)]*\)", "", n)  # strip all parenthetical qualifiers
    n = re.sub(r"\b(HD|SD|FHD|East|West|Pacific|Channel|Network|Feed|US)\b", "", n, flags=re.I)
    n = re.sub(r"[^a-z0-9]", "", n.lower())
    return n


def region_hint(name):
    nl = name.lower()
    return "west" if ("pacific" in nl or "west" in nl) else "east"


def slugify(name, slug_prefix):
    n = normalize(name)
    return f"{slug_prefix}-{n}" if n else f"{slug_prefix}-unknown"


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


def match_channel(display, source_by_norm, aliases):
    norm = normalize(display)
    hint = region_hint(display)
    target = aliases.get(norm, norm)
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
    we actually need, without holding the whole document in memory as a
    parsed tree."""
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

def build_output(entries_with_match, programmes_by_tag):
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
        progs = programmes_by_tag.get(e["tag"], {}).get(e["source_id"], [])
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

    log("Fetching playlist ...")
    m3u_text = fetch(M3U_URL)

    # Parse every target's group out of the playlist.
    for t in TARGETS:
        t["entries"] = parse_m3u_group(m3u_text, t["group_title"])
        log(f"Found {len(t['entries'])} entries in group {t['group_title']!r}.")

    # Fetch each source tag's channel index exactly once, even if it's
    # shared by more than one target (e.g. US2 for both US Movies and
    # US Documentaries).
    needed_tags = sorted(set(t["tag"] for t in TARGETS))
    source_by_norm_by_tag = {}
    for tag in needed_tags:
        log(f"Fetching source channel index for {tag} ...")
        index_text = fetch(SOURCE_TAGS[tag]["index_url"])
        source_by_norm_by_tag[tag] = build_source_index(index_text)

    # Match each target's entries against its own source tag + alias table.
    for t in TARGETS:
        matched, unmatched = [], []
        for e in t["entries"]:
            src = match_channel(e["display"], source_by_norm_by_tag[t["tag"]], ALIASES[t["tag"]])
            output_id = e["tvg_id"] if e["tvg_id"] else slugify(e["display"], t["slug"])
            if src:
                matched.append({**e, "source_id": src, "output_id": output_id, "tag": t["tag"]})
            else:
                unmatched.append(e)
        t["matched"] = matched
        t["unmatched"] = unmatched
        t["distinct_sources"] = sorted(set(m["source_id"] for m in matched))
        log(f"[{t['label']}] Matched {len(matched)} entries to {len(t['distinct_sources'])} distinct real source channels; {len(unmatched)} unmatched.")

    # Fetch + stream each needed source tag's big XML exactly once, pulling
    # only the programmes for the union of source ids needed across all
    # targets that share that tag.
    programmes_by_tag = {}
    for tag in needed_tags:
        wanted = set()
        for t in TARGETS:
            if t["tag"] == tag:
                wanted.update(t["distinct_sources"])
        log(f"Downloading + streaming the {tag} source guide ...")
        xml_gz_bytes = fetch(SOURCE_TAGS[tag]["xml_url"], binary=True)
        programmes_by_tag[tag] = extract_programmes(xml_gz_bytes, wanted)
        total = sum(len(v) for v in programmes_by_tag[tag].values())
        log(f"[{tag}] Pulled {total} programme entries.")

    for t in TARGETS:
        t["total_progs"] = sum(
            len(programmes_by_tag[t["tag"]].get(m["source_id"], [])) for m in t["matched"]
        )

    all_matched = [m for t in TARGETS for m in t["matched"]]
    tv = build_output(all_matched, programmes_by_tag)
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")

    out_xml_path = os.path.join(OUTPUT_DIR, "epg.xml")
    out_gz_path = os.path.join(OUTPUT_DIR, "epg.xml.gz")
    with open(out_xml_path, "wb") as f:
        f.write(xml_bytes)
    write_gzip(out_gz_path, xml_bytes)
    log(f"Wrote {out_xml_path} ({len(xml_bytes)} bytes) and {out_gz_path}.")

    # --- match report ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_entries = sum(len(t["entries"]) for t in TARGETS)
    total_matched = sum(len(t["matched"]) for t in TARGETS)
    total_progs = sum(t["total_progs"] for t in TARGETS)

    report_lines = [
        "# EPG build report",
        "",
        f"Last built: {now}",
        "",
        f"- Categories covered: {', '.join(t['label'] for t in TARGETS)}",
        f"- Total playlist entries across all categories: {total_entries}",
        f"- Total matched to a real published schedule: {total_matched}",
        f"- Total programme entries written: {total_progs}",
        "",
    ]

    for t in TARGETS:
        matched = t["matched"]
        unmatched = t["unmatched"]
        report_lines += [
            f"## {t['label']}",
            "",
            f"- Playlist entries in \"{t['group_title']}\": {len(t['entries'])} (after de-duplicating and dropping CINEBOX-style generic channels)",
            f"- Matched to a real published schedule: {len(matched)}",
            f"- Distinct real source channels used: {len(t['distinct_sources'])}",
            f"- Programme entries written: {t['total_progs']}",
            f"- Unmatched (no real-world schedule available, skipped): {len(unmatched)}",
            "",
            "### Unmatched channels",
            "",
        ]
        if unmatched:
            for e in unmatched:
                tag = e["tvg_id"] or "(no tvg-id)"
                report_lines.append(f"- `{tag}` - {e['display']}")
        else:
            report_lines.append("_None - everything matched!_")
        report_lines += [
            "",
            "### Channels without a tvg-id in the playlist",
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
        report_lines.append("")

    with open(os.path.join(OUTPUT_DIR, "match-report.md"), "w") as f:
        f.write("\n".join(report_lines) + "\n")

    # --- tiny status page ---
    rows = "".join(
        f"<tr><td>{t['label']}</td><td>{len(t['matched'])} / {len(t['entries'])}</td>"
        f"<td>{len(t['distinct_sources'])}</td><td>{t['total_progs']}</td></tr>"
        for t in TARGETS
    )
    index_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Personal EPG</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;line-height:1.5}}
code{{background:#f0f0f0;padding:2px 6px;border-radius:4px}}
table{{border-collapse:collapse;width:100%;margin-top:12px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #ddd}}</style></head>
<body>
<h1>Personal EPG</h1>
<p>Last built: {now}</p>
<p><a href="epg.xml">epg.xml</a> &middot; <a href="epg.xml.gz">epg.xml.gz</a> (gzip, smaller/faster to load)</p>
<table>
<tr><th>Category</th><th>Matched / Total</th><th>Source channels</th><th>Programmes</th></tr>
{rows}
</table>
<p>{total_matched} of {total_entries} channels matched to real schedules across all categories ({total_progs} programme entries).</p>
<p>See <a href="match-report.md">match-report.md</a> for exactly which channels matched and which didn't, broken out per category.</p>
</body></html>
"""
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w") as f:
        f.write(index_html)

    log("Done.")


if __name__ == "__main__":
    main()
