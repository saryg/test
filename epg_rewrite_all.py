#!/usr/bin/env python3
"""
Rewrites epgshare01's combined ALL_SOURCES XMLTV guide so channel IDs match
iptv-org's tvg-id scheme, covering every country at once (not just UK/IE/DE).

How it works:
    1. Downloads epg_ripper_ALL_SOURCES1.xml.gz (epgshare01's everything-file).
    2. For each channel, guesses its country from the trailing ".xx" code in
       its epgshare id (e.g. "Sky.Greats.HD.uk" -> "uk").
    3. Lazily downloads that country's iptv-org playlist (only once per
       country actually present in the data) and builds a name index.
    4. Matches by normalized channel name (exact, then fuzzy) same as the
       single-country script, and rewrites ids + programme refs.
    5. Countries with no iptv-org playlist, or with an id it can't parse a
       country out of, are left alone and logged to skipped_countries.csv.

This is a heavier job than the UK/IE/DE-only version: ALL_SOURCES1.xml.gz is
~200 MB compressed, and this script may end up downloading dozens of
separate iptv-org playlists. Expect a multi-minute run, not seconds -- fine
for a scheduled GitHub Action, not something to run interactively often.

Usage:
    python3 rewrite_epg_all.py
"""

import gzip
import re
import sys
import csv
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


def maybe_gunzip(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


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


def main():
    review_rows = []
    unmatched_rows = []
    skipped_country_rows = []
    id_map = {}

    print("Downloading ALL_SOURCES guide (this is large, ~200MB compressed)...", file=sys.stderr)
    raw = maybe_gunzip(fetch(ALL_SOURCES_URL))
    print("Parsing XML...", file=sys.stderr)
    tree = ET.fromstring(raw)

    cache = CountryIndexCache()
    matched = skipped = unmatched = no_country = 0

    channel_els = tree.findall("channel")
    print(f"{len(channel_els)} channels in ALL_SOURCES file", file=sys.stderr)

    for i, channel_el in enumerate(channel_els):
        if i % 2000 == 0 and i:
            print(f"  ...{i}/{len(channel_els)} channels processed", file=sys.stderr)

        old_id = channel_el.get("id", "")
        display_name_el = channel_el.find("display-name")
        display_name = display_name_el.text if display_name_el is not None else old_id

        cc = guess_country_code(old_id)
        if not cc:
            no_country += 1
            skipped_country_rows.append([old_id, display_name, "no country code parsed from id"])
            continue

        name_index = cache.get(cc)
        if name_index is None:
            skipped_country_rows.append([old_id, display_name, f"no iptv-org playlist for '{cc}'"])
            continue

        key = normalize(display_name)
        if not key:
            unmatched += 1
            continue

        if key in name_index:
            new_id = name_index[key]
            id_map[old_id] = new_id
            channel_el.set("id", new_id)
            matched += 1
            continue

        lookup_keys = list(name_index.keys())
        result = process.extractOne(key, lookup_keys, scorer=fuzz.WRatio) if lookup_keys else None
        if result:
            best_key, score, _ = result
            if score >= MATCH_THRESHOLD:
                new_id = name_index[best_key]
                id_map[old_id] = new_id
                channel_el.set("id", new_id)
                matched += 1
                continue
            elif score >= REVIEW_THRESHOLD:
                review_rows.append([cc, old_id, display_name, name_index[best_key], best_key, score])
                skipped += 1
                continue

        unmatched_rows.append([cc, old_id, display_name])
        unmatched += 1

    for programme_el in tree.findall("programme"):
        ch = programme_el.get("channel", "")
        if ch in id_map:
            programme_el.set("channel", id_map[ch])

    print(
        f"\nDone matching: matched={matched} review={skipped} unmatched={unmatched} "
        f"no_country_or_playlist={len(skipped_country_rows)}",
        file=sys.stderr,
    )

    ET.ElementTree(tree).write("guide_all.xml", encoding="UTF-8", xml_declaration=True)

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
