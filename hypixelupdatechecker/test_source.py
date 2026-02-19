"""
Standalone scraping test — no Discord bot or Red needed.

Run with:  python test_scraper.py
Requires:  pip install aiohttp beautifulsoup4

Pass --debug to also dump raw HTML snippets when no threads are found.
"""

import asyncio
import sys
import aiohttp
from hypixelupdatechecker import (
    SOURCES,
    _fetch_html,
    _parse_thread_list,
    _parse_post_content,
    _THREAD_URL_RE,
)
from bs4 import BeautifulSoup

DEBUG = "--debug" in sys.argv


async def test_source(session: aiohttp.ClientSession, source_key: str):
    source_cfg = SOURCES[source_key]
    print(f"\n{'='*60}")
    print(f"SOURCE: {source_key} — {source_cfg['label']}")
    print(f"URL:    {source_cfg['url']}")
    print("="*60)

    html = await _fetch_html(session, source_cfg["url"])
    if not html:
        print("  ❌ Failed to fetch listing page!")
        return

    # ── Debug: show what raw thread links look like before filtering ──────
    soup = BeautifulSoup(html, "html.parser")
    all_thread_links = soup.find_all("a", href=_THREAD_URL_RE)
    print(f"  Raw thread links in HTML (before filters): {len(all_thread_links)}")

    if DEBUG and not all_thread_links:
        print("\n  --- RAW HTML SNIPPET (first 2000 chars) ---")
        print(html[:2000])
        print("  ---")

    if all_thread_links:
        print("  First 3 raw links found:")
        for link in all_thread_links[:3]:
            print(f"    {link['href']!r:60s}  text={link.get_text(strip=True)[:50]!r}")

    # ── Now run with filters ──────────────────────────────────────────────
    threads = _parse_thread_list(html, source_cfg)

    if not threads:
        print(f"\n  ⚠️  0 threads after filters!")
        if source_cfg["require_hypixel_team"]:
            from hypixelupdatechecker import _find_container, HYPIXEL_TEAM_MEMBER_PATH

            # Print the ancestor chain for the first thread link so we can
            # see exactly what tags/attributes exist and where data-author lives
            if all_thread_links:
                print("\n  --- Ancestor chain for first thread link ---")
                node = all_thread_links[0]
                for i in range(15):
                    node = node.parent
                    if node is None or node.name in ("body", "html", "[document]"):
                        break
                    classes = " ".join(node.get("class", []))[:60]
                    data_auth = node.get("data-author", "")
                    member_links = node.find_all("a", href=lambda h: h and "/members/" in h)
                    member_hrefs = [a["href"] for a in member_links][:3]
                    print(f"    [{i+1:2d}] <{node.name}> class={classes!r:62s} data-author={data_auth!r:20s} member_links={member_hrefs}")
                print("  ---\n")
        return

    print(f"\n  ✅ Found {len(threads)} matching thread(s)\n")
    for t in threads[:5]:
        sticky = "📌" if t["is_sticky"] else "  "
        official = "✔" if t["is_official"] else " "
        print(f"  {sticky} [{official}] [{t['thread_id']}] {t['title'][:60]}")
        print(f"              {t['url']}")

    # ── Deep-fetch the first thread ───────────────────────────────────────
    first = threads[0]
    print(f"\n  --- Fetching post content: {first['title'][:50]} ---")
    thread_html = await _fetch_html(session, first["url"])
    if not thread_html:
        print("  ❌ Failed to fetch thread page!")
        return

    post = _parse_post_content(thread_html)
    print(f"\n  Preview ({len(post['preview'])} chars):")
    print(f"  {post['preview'][:300]!r}")
    print(f"\n  Spoiler sections ({len(post['spoilers'])}):")
    for s in post["spoilers"]:
        print(f"    ▸ {s}")
    print(f"\n  Content hash: {post['raw_hash']}")

    if DEBUG and not post["preview"]:
        print("\n  --- THREAD HTML SNIPPET (first 2000 chars) ---")
        print(thread_html[:2000])


async def main():
    async with aiohttp.ClientSession() as session:
        for key in SOURCES:
            await test_source(session, key)
            await asyncio.sleep(1)

    print("\n\nDone.")
    print("If sources show 0 threads, re-run with --debug to see raw HTML.")


if __name__ == "__main__":
    asyncio.run(main())