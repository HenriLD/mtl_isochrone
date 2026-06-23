"""Download + thumbnail the quest images and capture attribution.

For each quest whose `image` is a remote Wikimedia Commons URL: download it,
resize to ~400 px wide JPEG into web/images/quests/<id>.jpg, fetch the
artist/licence (Commons API extmetadata), and rewrite the quest's `image` to the
local path + fill `attribution`. Failures just null the image. Idempotent: skips
quests already pointing at a local file.

Run:  python scripts/fetch_quest_images.py
"""
from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
QUESTS = ROOT / "web" / "side_quests.json"
IMG_DIR = ROOT / "web" / "images" / "quests"
UA = "mtl-isochrone-sidequests/1.0 (henrildu20@gmail.com)"
_TAGS = re.compile(r"<[^>]+>")


def _get(url: str, timeout: int = 40, retries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  429 — backing off {wait}s")
                time.sleep(wait)
                continue
            raise


def _filename(commons_url: str) -> str | None:
    m = re.search(r"Special:FilePath/([^?]+)", commons_url)
    return urllib.parse.unquote(m.group(1)) if m else None


def _attribution(filename: str) -> str:
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "imageinfo",
        "iiprop": "extmetadata", "titles": f"File:{filename}",
    })
    try:
        js = json.loads(_get(f"https://commons.wikimedia.org/w/api.php?{params}", timeout=30))
        page = next(iter(js["query"]["pages"].values()))
        ext = page["imageinfo"][0]["extmetadata"]
        artist = _TAGS.sub("", (ext.get("Artist", {}) or {}).get("value", "")).strip()
        lic = (ext.get("LicenseShortName", {}) or {}).get("value", "").strip()
        bits = [b for b in (artist, lic) if b]
        return ("Photo: " + " / ".join(bits) + " (Wikimedia Commons)") if bits else "Wikimedia Commons"
    except Exception:
        return "Wikimedia Commons"


def main() -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    quests = json.loads(QUESTS.read_text(encoding="utf-8"))
    ok = fail = skip = 0
    for q in quests:
        img = q.get("image")
        if not img or not img.startswith("http"):
            if img and not img.startswith("http"):
                skip += 1
            continue
        fn = _filename(img)
        if not fn:
            q["image"] = None
            continue
        dest = IMG_DIR / f"{q['id']}.jpg"
        try:
            if not dest.exists():                    # reuse already-downloaded files
                data = _get(img)
                im = Image.open(io.BytesIO(data)).convert("RGB")
                w, h = im.size
                if w > 400:
                    im = im.resize((400, round(h * 400 / w)), Image.LANCZOS)
                im.save(dest, quality=82, optimize=True, progressive=True)
                time.sleep(1.0)
            q["image"] = f"images/quests/{q['id']}.jpg"
            q["attribution"] = _attribution(fn)
            time.sleep(0.8)
            ok += 1
            if ok % 15 == 0:
                print(f"  {ok} done...")
        except Exception as e:
            print(f"  fail {q['id']} ({q['name']}): {e}")
            q["image"] = None
            fail += 1
    QUESTS.write_text(json.dumps(quests, ensure_ascii=False, indent=1), encoding="utf-8")
    total_kb = sum(p.stat().st_size for p in IMG_DIR.glob("*.jpg")) / 1024
    print(f"Done: {ok} downloaded, {fail} failed, {skip} already local. "
          f"{len(list(IMG_DIR.glob('*.jpg')))} files, {total_kb:.0f} KB total.")


if __name__ == "__main__":
    main()
