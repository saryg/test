#!/usr/bin/env python3
"""
Rewrites epgshare01's combined ALL_SOURCES XMLTV guide so channel IDs match
iptv-org's tvg-id scheme, covering every country at once.
 
How it works:
    1. Downloads epg_ripper_ALL_SOURCES1.xml.gz (epgshare01's everything-file).
    2. For each channel, guesses its country from the trailing ".xx" code in
       its epgshare id.
    3. Lazily downloads that country's iptv-org playlist (only once per
       country actually present in the data) and builds a name index.
    4. Matches by normalized channel name (exact, then fuzzy) same as the
       single-country script, and rewrites ids + programme refs.
    5. Countries with no iptv-org playlist, or with an id it can't parse a
       country out of, are left alone and logged to skipped_countries.csv.
"""
 
import gzip
import os
import re
import sys
import csv
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from rapidfuzz import fuzz, process
 
# ── Config ──────────────────────────────────────────────────────────────
ALL_SOURCES_URL = "https://epgshare01.online/epgshare01/epg_ripper_ALL_SOURCES1.xml.gz"
PLAYLIST_URL_TEMPLATE = "https://iptv-org.github.io/iptv/countries/{cc}.m3u"
 
MATCH_THRESHOLD = 85
REVIEW_THRESHOLD = 65
USER_AGENT = "Mozilla/5.0 (compatible; epg-rewrite-script/1.0)"
 
# epgshare's trailing country code -> iptv-org's country code, where they
# differ. iptv-org mostly uses standard lowercase ISO codes but keeps a few
# historical exceptions (uk instead of gb, etc). Extend this if you spot
# more mismatches in skipped_countries.csv after a run.
COUNTRY_CODE_OVERRIDES = {
    "uk": "uk",   # iptv-org keeps "uk", not "gb"
}
 
NOISE_WORDS = re.compile(
    r"\b(HD|SD|FHD|UHD|4K|PLUS1|\+1|EAST|WEST|HEVC|FALLBACK)\b",
    re.IGNORECASE,
)
PAREN_BRACKET = re.compile(r"\([^)]*\)|\[[^\]]*\]")
NON_ALNUM = re.compile(r"[^a-z0-9]+")
COUNTRY_SUFFIX = re.compile(r"\.([a-zA-Z]{2})$")
 
 
def normalize(name: str) -> str:
    name = name.strip()
    name = PAREN_BRACKET.sub(" ", name)
    name = NOISE_WORDS.sub(" ", name)
    name = name.lower()
    name = NON_ALNUM.sub("", name)
    return name
 
 
def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()
 
 
def download_to_file(url: str, dest_path: str, chunk_size: int = 1024 * 1024) -> None:
    """Streams a URL straight to disk instead of buffering it all in memory --
    important for the ~200MB ALL_SOURCES file."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest_path, "wb") as out:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
 
 
def open_maybe_gzip(path: str):
    """Returns a file-like object for streaming-reading path, transparently
    decompressing if it's gzipped. Never loads the whole file into memory."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")
 
 
def guess_country_code(epgshare_id: str) -> str | None:
    m = COUNTRY_SUFFIX.search(epgshare_id)
    if not m:
        return None
    code = m.group(1).lower()
    return COUNTRY_CODE_OVERRIDES.get(code, code)
 
 
def parse_m3u_channels(m3u_text: str):
    channels = []
    tvg_id_re = re.compile(r'tvg-id="([^"]*)"')
    name_re = re.compile(r',\s*(.+)$')
    for line in m3u_text.splitlines():
        if line.startswith("#EXTINF"):
            tvg_id_match = tvg_id_re.search(line)
            name_match = name_re.search(line)
            if tvg_id_match and name_match and tvg_id_match.group(1):
                channels.append((tvg_id_match.group(1), name_match.group(1).strip()))
    return channels
 
 
def build_name_index(playlist_channels):
    index = {}
    for tvg_id, name in playlist_channels:
        key = normalize(name)
        if key and key not in index:
            index[key] = tvg_id
    return index
 
 
class CountryIndexCache:
    """Lazily downloads + caches an iptv-org playlist's name index per country."""
 
    def __init__(self):
        self._cache = {}         # cc -> dict[normalized_name] = tvg_id  (or None if unavailable)
        self.unavailable = set()
 
    def get(self, cc: str):
        if cc in self._cache:
            return self._cache[cc]
        url = PLAYLIST_URL_TEMPLATE.format(cc=cc)
        try:
            print(f"  downloading playlist for '{cc}'...", file=sys.stderr)
            text = fetch(url).decode("utf-8", errors="replace")
            channels = parse_m3u_channels(text)
            index = build_name_index(channels)
            print(f"  '{cc}': {len(channels)} playlist channels", file=sys.stderr)
        except Exception as e:
            print(f"  '{cc}': playlist unavailable ({e})", file=sys.stderr)
            index = None
            self.unavailable.add(cc)
        self._cache[cc] = index
        return index
 
 
def match_channel(old_id, display_name, cc, cache, id_map, review_rows, unmatched_rows):
    """Returns the new id if matched/auto-applied, else None (and logs it)."""
    name_index = cache.get(cc)
    if name_index is None:
        return None
 
    key = normalize(display_name)
    if not key:
        unmatched_rows.append([cc, old_id, display_name])
        return None
 
    if key in name_index:
        new_id = name_index[key]
        id_map[old_id] = new_id
        return new_id
 
    lookup_keys = list(name_index.keys())
    result = process.extractOne(key, lookup_keys, scorer=fuzz.WRatio) if lookup_keys else None
    if result:
        best_key, score, _ = result
        if score >= MATCH_THRESHOLD:
            new_id = name_index[best_key]
            id_map[old_id] = new_id
            return new_id
        elif score >= REVIEW_THRESHOLD:
            review_rows.append([cc, old_id, display_name, name_index[best_key], best_key, score])
            return None
 
    unmatched_rows.append([cc, old_id, display_name])
    return None
 
 
def main():
    review_rows = []
    unmatched_rows = []
    skipped_country_rows = []
    id_map = {}
    cache = CountryIndexCache()
    matched = 0
 
    tmpdir = tempfile.mkdtemp(prefix="epg-rewrite-")
    gz_path = os.path.join(tmpdir, "all_sources.xml.gz")
    channels_out_path = os.path.join(tmpdir, "channels.xml")
    programmes_out_path = os.path.join(tmpdir, "programmes.xml")
 
    print("Downloading ALL_SOURCES guide (this is large, ~200MB compressed)...", file=sys.stderr)
    download_to_file(ALL_SOURCES_URL, gz_path)
 
    print("Streaming through XML (single pass -- never holds the whole file in memory)...", file=sys.stderr)
    # XMLTV files list every <channel> before any <programme>, so a single
    # forward streaming pass can build id_map from channels first, then use
    # it immediately once programme elements start appearing.
    count = 0
    with open_maybe_gzip(gz_path) as xml_stream, \
         open(channels_out_path, "w", encoding="utf-8") as channels_out, \
         open(programmes_out_path, "w", encoding="utf-8") as programmes_out:
 
        context = ET.iterparse(xml_stream, events=("start", "end"))
        _, root = next(context)  # first event is the <tv> root's "start"
 
        for event, elem in context:
            if event != "end":
                continue
            tag = elem.tag
            if tag == "channel":
                count += 1
                if count % 5000 == 0:
                    print(f"  ...{count} channels processed", file=sys.stderr)
 
                old_id = elem.get("id", "")
                display_name_el = elem.find("display-name")
                display_name = display_name_el.text if display_name_el is not None else old_id
 
                cc = guess_country_code(old_id)
                if not cc:
                    skipped_country_rows.append([old_id, display_name, "no country code parsed from id"])
                else:
                    new_id = match_channel(old_id, display_name, cc, cache, id_map, review_rows, unmatched_rows)
                    if new_id:
                        elem.set("id", new_id)
                        matched += 1
                    elif cache.get(cc) is None:
                        skipped_country_rows.append([old_id, display_name, f"no iptv-org playlist for '{cc}'"])
 
                channels_out.write(ET.tostring(elem, encoding="unicode"))
                elem.clear()
                root.clear()  # drop the now-empty stub root would otherwise keep referencing
 
            elif tag == "programme":
                ch = elem.get("channel", "")
                if ch in id_map:
                    elem.set("channel", id_map[ch])
                programmes_out.write(ET.tostring(elem, encoding="unicode"))
                elem.clear()
                root.clear()
 
    print(
        f"\nDone matching: matched={matched} review={len(review_rows)} "
        f"unmatched={len(unmatched_rows)} no_country_or_playlist={len(skipped_country_rows)}",
        file=sys.stderr,
    )
 
    print("Writing combined guide_all.xml...", file=sys.stderr)
    with open("guide_all.xml", "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write('<tv generator-info-name="epg-rewrite-script">\n')
        with open(channels_out_path, encoding="utf-8") as f:
            for line in f:
                out.write(line)
        with open(programmes_out_path, encoding="utf-8") as f:
            for line in f:
                out.write(line)
        out.write("</tv>\n")
 
    with open("review_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["country", "epgshare_id", "epgshare_name", "candidate_tvg_id", "candidate_key", "score"])
        w.writerows(review_rows)
 
    with open("unmatched_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["country", "epgshare_id", "epgshare_name"])
        w.writerows(unmatched_rows)
 
    with open("skipped_countries_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["epgshare_id", "epgshare_name", "reason"])
        w.writerows(skipped_country_rows)
 
    print(
        f"Wrote guide_all.xml, review_all.csv ({len(review_rows)}), "
        f"unmatched_all.csv ({len(unmatched_rows)}), "
        f"skipped_countries_all.csv ({len(skipped_country_rows)}).",
        file=sys.stderr,
    )
 
 
if __name__ == "__main__":
    main()
 


Downloaded epg-rewrite-script.zip Show in Explorer
