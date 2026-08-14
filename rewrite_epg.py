#!/usr/bin/env python3
"""
Rewrites epgshare01 XMLTV channel IDs to match iptv-org's tvg-id scheme,
by fuzzy-matching channel display names, so exact-ID EPG matching works
in IPTV players (TiviMate, StreamVault, etc.) against iptv-org playlists.

Usage:
    python3 rewrite_epg.py

Reads config below (COUNTRIES), downloads iptv-org playlists + epgshare01
guides, matches, rewrites, and writes:
    - guide.xml       combined XMLTV with corrected channel ids
    - review.csv       low-confidence matches to manually check
    - unmatched.csv     epgshare01 channels that found no reasonable match
"""

import gzip
import io
import re
import sys
import csv
import urllib.request
import xml.etree.ElementTree as ET
from rapidfuzz import fuzz, process

# ── Config ──────────────────────────────────────────────────────────────
COUNTRIES = {
    "uk": {
        "m3u_url": "https://iptv-org.github.io/iptv/countries/uk.m3u",
        "epg_url": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    },
    "ie": {
        "m3u_url": "https://iptv-org.github.io/iptv/countries/ie.m3u",
        "epg_url": "https://epgshare01.online/epgshare01/epg_ripper_IE1.xml.gz",
    },
    "de": {
        "m3u_url": "https://iptv-org.github.io/iptv/countries/de.m3u",
        "epg_url": "https://epgshare01.online/epgshare01/epg_ripper_DE1.xml.gz",
    },
}

MATCH_THRESHOLD = 85       # >= this score: auto-applied
REVIEW_THRESHOLD = 65      # between this and MATCH_THRESHOLD: logged for review, not applied
USER_AGENT = "Mozilla/5.0 (compatible; epg-rewrite-script/1.0)"

NOISE_WORDS = re.compile(
    r"\b(HD|SD|FHD|UHD|4K|PLUS1|\+1|EAST|WEST|UK|IE|DE|HEVC|FALLBACK)\b",
    re.IGNORECASE,
)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


PAREN_BRACKET = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def normalize(name: str) -> str:
    name = name.strip()
    name = PAREN_BRACKET.sub(" ", name)   # strip "(1080p)", "[Not 24/7]", etc.
    name = NOISE_WORDS.sub(" ", name)
    name = name.lower()
    name = NON_ALNUM.sub("", name)
    return name


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def maybe_gunzip(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def parse_m3u_channels(m3u_text: str):
    """Returns list of (tvg_id, display_name)."""
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


def load_country(code: str, cfg: dict):
    print(f"[{code}] downloading playlist...", file=sys.stderr)
    m3u_text = fetch(cfg["m3u_url"]).decode("utf-8", errors="replace")
    playlist_channels = parse_m3u_channels(m3u_text)
    print(f"[{code}] {len(playlist_channels)} playlist channels found", file=sys.stderr)

    print(f"[{code}] downloading epgshare01 guide...", file=sys.stderr)
    raw = maybe_gunzip(fetch(cfg["epg_url"]))
    tree = ET.fromstring(raw)
    return playlist_channels, tree


def build_name_index(playlist_channels):
    """normalized_name -> tvg_id (first occurrence wins), plus lookup list for fuzzy search."""
    index = {}
    for tvg_id, name in playlist_channels:
        key = normalize(name)
        if key and key not in index:
            index[key] = tvg_id
    return index


def rewrite_country(code: str, cfg: dict, review_rows: list, unmatched_rows: list):
    playlist_channels, tree = load_country(code, cfg)
    name_index = build_name_index(playlist_channels)
    lookup_keys = list(name_index.keys())

    id_map = {}  # epgshare_id -> iptv_org_tvg_id
    matched = skipped = unmatched = 0

    for channel_el in tree.findall("channel"):
        old_id = channel_el.get("id", "")
        display_name_el = channel_el.find("display-name")
        display_name = display_name_el.text if display_name_el is not None else old_id
        key = normalize(display_name)

        if not key:
            unmatched += 1
            continue

        # Exact normalized match first
        if key in name_index:
            new_id = name_index[key]
            id_map[old_id] = new_id
            channel_el.set("id", new_id)
            matched += 1
            continue

        # Fuzzy fallback
        result = process.extractOne(key, lookup_keys, scorer=fuzz.WRatio)
        if result:
            best_key, score, _ = result
            if score >= MATCH_THRESHOLD:
                new_id = name_index[best_key]
                id_map[old_id] = new_id
                channel_el.set("id", new_id)
                matched += 1
                continue
            elif score >= REVIEW_THRESHOLD:
                review_rows.append([code, old_id, display_name, name_index[best_key], best_key, score])
                skipped += 1
                continue

        unmatched_rows.append([code, old_id, display_name])
        unmatched += 1

    # Rewrite programme channel refs to match
    for programme_el in tree.findall("programme"):
        ch = programme_el.get("channel", "")
        if ch in id_map:
            programme_el.set("channel", id_map[ch])

    print(
        f"[{code}] matched={matched} review={skipped} unmatched={unmatched} "
        f"(of {matched + skipped + unmatched} epgshare channels)",
        file=sys.stderr,
    )
    return tree


def merge_trees(trees):
    root = ET.Element("tv", attrib={"generator-info-name": "epg-rewrite-script"})
    seen_channel_ids = set()
    for tree in trees:
        for channel_el in tree.findall("channel"):
            cid = channel_el.get("id")
            if cid in seen_channel_ids:
                continue
            seen_channel_ids.add(cid)
            root.append(channel_el)
    for tree in trees:
        for programme_el in tree.findall("programme"):
            root.append(programme_el)
    return ET.ElementTree(root)


def main():
    review_rows = []
    unmatched_rows = []
    trees = []

    for code, cfg in COUNTRIES.items():
        try:
            trees.append(rewrite_country(code, cfg, review_rows, unmatched_rows))
        except Exception as e:
            print(f"[{code}] FAILED: {e}", file=sys.stderr)

    merged = merge_trees(trees)
    merged.write("guide.xml", encoding="UTF-8", xml_declaration=True)

    with open("review.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["country", "epgshare_id", "epgshare_name", "candidate_tvg_id", "candidate_key", "score"])
        w.writerows(review_rows)

    with open("unmatched.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["country", "epgshare_id", "epgshare_name"])
        w.writerows(unmatched_rows)

    print(f"\nDone. Wrote guide.xml, review.csv ({len(review_rows)} rows), "
          f"unmatched.csv ({len(unmatched_rows)} rows).", file=sys.stderr)


if __name__ == "__main__":
    main()
