import argparse
import json
import logging
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from db import (
    complete_crawl_run,
    connect_db,
    create_crawl_run,
    ensure_schema,
    get_active_groups,
    get_database_url,
    get_live_cookie_json,
    ingest_post,
    mark_cookie_dead,
    update_group_crawled,
)


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_COOKIE_FILE = ROOT_DIR / "cookies.json"
DEFAULT_GROUP_FILE = ROOT_DIR / "facebook_groups.txt"
DEFAULT_PROFILE_DIR = ROOT_DIR / "browser_profile_desktop"
FEED_SELECTOR = "[role='feed']"
BDS_KEYWORDS = (
    "bán",
    "cho thuê",
    "đất",
    "nhà",
    "mặt tiền",
    "lô",
    "nền",
    "thanh hóa",
    "triệu",
    "tỷ",
    "m²",
    "m2",
)
DATETIME_PATTERN = re.compile(r"(Vừa xong|\d+\s*(?:phút|giờ|ngày|tuần|tháng|năm))", re.IGNORECASE)
AUTHOR_PATTERN = re.compile(r"^[A-ZÀ-Ỹ][A-Za-zÀ-ỹ0-9 .,'-]{1,80}$")
AUTHOR_BADGES = {
    "Rất nhiệt tình",
    "Người đóng góp hàng đầu",
    "Người đóng góp nổi bật",
    "Tác giả",
    "Người kiểm duyệt",
    "Không có mô tả ảnh.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape bai viet group Facebook bang Playwright desktop-first.")
    parser.add_argument("--groups-file", default=None)
    parser.add_argument("--group-url", default=None)
    parser.add_argument("--cookies-file", default=None)
    parser.add_argument("--use-db-cookies", action="store_true", help="Lay cookie tu DB thay vi file")
    parser.add_argument("--use-db-groups", action="store_true", help="Lay groups tu DB thay vi file")
    parser.add_argument("--output-dir", default=None, help="Thu muc luu artifact/debug (tuy chon)")
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--max-groups", type=int, default=None)
    parser.add_argument("--scroll-rounds", type=int, default=16)
    parser.add_argument("--scroll-pause-ms", type=int, default=2800)
    parser.add_argument("--scroll-px", type=int, default=1800)
    parser.add_argument("--expand-rounds", type=int, default=5)
    parser.add_argument("--max-posts", type=int, default=1000)
    parser.add_argument("--max-posts-per-group", type=int, default=120)
    parser.add_argument("--min-content-length", type=int, default=30)
    parser.add_argument("--near-duplicate-threshold", type=float, default=0.88)
    parser.add_argument("--keyword-filter", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--workers", type=int, default=2, help="So luong browser chay song song (mac dinh: 2)")
    return parser


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")


def load_group_urls(groups_file: Path, max_groups: int | None) -> list[str]:
    if not groups_file.exists():
        raise FileNotFoundError(f"Khong tim thay file groups: {groups_file}")
    urls: list[str] = []
    for raw_line in groups_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    if max_groups is not None:
        urls = urls[:max_groups]
    if not urls:
        raise ValueError("Khong co URL group nao trong file input.")
    return urls


def normalize_same_site(value: str | None) -> str:
    normalized = (value or "lax").lower().strip()
    return {"unspecified": "Lax", "lax": "Lax", "strict": "Strict", "no_restriction": "None", "none": "None"}.get(normalized, "Lax")


def _transform_db_cookies(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize cookies from DB into Playwright-compatible format."""
    now = time.time()
    cookies: list[dict[str, Any]] = []
    for item in raw:
        name = item.get("name", "?")
        cookie: dict[str, Any] = {
            "name": name,
            "value": item.get("value", ""),
            "domain": item.get("domain", ".facebook.com"),
            "path": item.get("path", "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
            "sameSite": normalize_same_site(item.get("sameSite")),
        }
        expires = item.get("expirationDate") or item.get("expires")
        is_session = bool(item.get("session"))
        if expires and not is_session:
            cookie["expires"] = int(expires)
            expiry_ts = int(expires)
            if expiry_ts < now:
                logging.error("[cookies] Cookie '%s' expired — Facebook auth will fail.", name)
        cookies.append(cookie)
    return cookies


def load_facebook_cookies(cookie_file: Path) -> list[dict[str, Any]]:
    payload = json.loads(cookie_file.read_text(encoding="utf-8"))
    cookie_items = payload.get("cookies", payload) if isinstance(payload, dict) else payload
    cookies: list[dict[str, Any]] = []
    now = time.time()
    warnings: list[str] = []
    for item in cookie_items:
        name = item.get("name", "?")
        cookie = {
            "name": name,
            "value": item.get("value", ""),
            "domain": item.get("domain", ".facebook.com"),
            "path": item.get("path", "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
            "sameSite": normalize_same_site(item.get("sameSite")),
        }
        expires = item.get("expirationDate") or item.get("expires")
        is_session = bool(item.get("session"))
        if expires and not is_session:
            cookie["expires"] = int(expires)
            expiry_ts = int(expires)
            if expiry_ts < now:
                logging.error(
                    "[cookies] Cookie '%s' expired on %s — Facebook auth will fail. "
                    "Refresh cookies.json immediately.",
                    name, datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat()
                )
            elif expiry_ts - now < 7 * 86400:
                warnings.append(
                    f"[cookies] Cookie '{name}' expires in "
                    f"{int((expiry_ts - now) / 86400)} days "
                    f"({datetime.fromtimestamp(expiry_ts, tz=timezone.utc).date()})"
                )
        if not expires and not is_session:
            logging.warning("[cookies] Cookie '%s' has no expiry — treating as session cookie.", name)
        cookies.append(cookie)

    for w in warnings:
        logging.warning(w)
    if not cookies:
        raise ValueError(f"No cookies loaded from {cookie_file}")
    return cookies


def safe_slug(url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-").lower()
    return slug[:80] or "facebook-group"


def normalize_text(value: str | None) -> str:
    text = (value or "").replace("\xa0", " ").replace("\u200e", "").replace("\u200f", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_author(line: str) -> bool:
    if line.startswith("Có thể là hình ảnh"):
        return False
    if not AUTHOR_PATTERN.fullmatch(line):
        return False
    lowered = line.lower()
    if any(token in lowered for token in ("facebook", "nhóm", "thành viên", "gợi ý", "đang tải")):
        return False
    if line in AUTHOR_BADGES:
        return False
    return True


def content_matches_keywords(content: str) -> bool:
    lowered = content.lower()
    return any(keyword in lowered for keyword in BDS_KEYWORDS)


def extract_author_id(url: str | None) -> str | None:
    if not url:
        return None
    for pattern in (r"/user/(\d+)", r"profile\.php\?id=(\d+)", r"[?&]id=(\d+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def normalize_post_url(url: str | None) -> str | None:
    if not url:
        return None
    absolute_url = urljoin("https://www.facebook.com", url)
    parsed = urlparse(absolute_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    query_params = dict(parse_qsl(parsed.query))

    if "story_fbid" in query_params and "id" in query_params:
        return f"https://www.facebook.com/groups/{query_params['id']}/posts/{query_params['story_fbid']}/"
    if "multi_permalinks" in query_params and path_parts[:1] == ["groups"] and len(path_parts) >= 2:
        return f"https://www.facebook.com/groups/{path_parts[1]}/posts/{query_params['multi_permalinks']}/"
    if path_parts[:2] == ["commerce", "listing"] and len(path_parts) >= 3:
        filtered_query: dict[str, str] = {}
        if "media_id" in query_params:
            filtered_query["media_id"] = query_params["media_id"]
        query = f"?{urlencode(filtered_query)}" if filtered_query else ""
        return f"https://www.facebook.com/commerce/listing/{path_parts[2]}{query}"
    if path_parts[:1] == ["groups"] and "posts" in path_parts:
        post_index = path_parts.index("posts")
        if len(path_parts) > post_index + 1 and len(path_parts) > 1:
            return f"https://www.facebook.com/groups/{path_parts[1]}/posts/{path_parts[post_index + 1]}/"
    if path_parts[:1] == ["groups"] and "permalink" in path_parts:
        group_id = path_parts[1] if len(path_parts) > 1 else ""
        story_id = query_params.get("story_fbid") or query_params.get("multi_permalinks", "")
        if group_id and story_id:
            return f"https://www.facebook.com/groups/{group_id}/posts/{story_id}/"
    return None


def extract_datetime_from_text(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = normalize_text(text)
    match = DATETIME_PATTERN.search(cleaned)
    if match:
        return match.group(1)
    simplified = re.sub(r"[^A-Za-zÀ-ỹ0-9 ]+", " ", cleaned)
    simplified = re.sub(r"\s+", " ", simplified).strip()
    match = DATETIME_PATTERN.search(simplified)
    if match:
        return match.group(1)
    odd_hour = re.search(r"(\d{1,2})\s*ờ", cleaned)
    if odd_hour:
        value = int(odd_hour.group(1))
        if value > 0:
            return f"{value} giờ"
    odd_min = re.search(r"(\d{1,2})\s*p", cleaned, re.IGNORECASE)
    if odd_min:
        value = int(odd_min.group(1))
        if value > 0:
            return f"{value} phút"
    return None


def looks_like_candidate_post_href(href: str) -> bool:
    if not href:
        return False
    lowered = href.lower()
    if "/commerce/listing/" in lowered:
        return False
    if "/user/" in lowered or "profile.php" in lowered:
        return False
    if lowered.startswith("?__cft__") or lowered.startswith("/groups/"):
        return True
    return False


def normalize_facebook_href(href: str | None) -> str | None:
    if not href:
        return None
    return href if href.startswith("http") else f"https://www.facebook.com{href}"


def clean_content_lines(lines: list[str], author: str | None, datetime_text: str | None) -> str:
    cleaned: list[str] = []
    for line in lines:
        line = normalize_text(line)
        if not line:
            continue
        without_see_more = re.sub(r"\s*…?\s*Xem thêm$", "", line, flags=re.IGNORECASE).strip()
        if not without_see_more:
            continue
        lowered = line.lower()
        if author and line == author:
            continue
        if datetime_text and datetime_text in line and len(line) < 40:
            continue
        if lowered == "facebook":
            continue
        if without_see_more in {"·", "•"}:
            continue
        if len(without_see_more) == 1:
            continue
        if re.fullmatch(r"[A-Za-z0-9]", without_see_more):
            continue
        if re.fullmatch(r"[A-Za-zÀ-ỹ0-9 ]{1,40}", without_see_more) and " " not in without_see_more and len(without_see_more) <= 20:
            continue
        if lowered in {
            "thích",
            "bình luận",
            "chia sẻ",
            "nhắn tin",
            "xem thêm",
            "ẩn bớt",
            "rất nhiệt tình",
            "gợi ý nhóm",
            "mở ứng dụng",
        }:
            continue
        if lowered.startswith("bình luận dưới tên"):
            break
        if lowered.startswith("tất cả cảm xúc"):
            break
        if re.fullmatch(r"[0-9]+\s*bình luận", lowered):
            break
        if re.fullmatch(r"[0-9.]+\s*₫", line):
            continue
        if re.fullmatch(r"[0-9.]+\s*us\$", lowered):
            continue
        if re.fullmatch(r"[0-9.]+\s*(triệu|tỷ)", lowered):
            continue
        if re.fullmatch(r"\+\d+", line):
            continue
        if line.endswith(", VIỆT NAM") or line.endswith(", Việt Nam"):
            continue
        if re.fullmatch(r"(mới|đã qua sử dụng.*|tình trạng)", lowered):
            continue
        if "thành viên" in lowered and "bài viế" in lowered:
            continue
        cleaned.append(without_see_more)

    deduped: list[str] = []
    seen: set[str] = set()
    for line in cleaned:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return "\n".join(deduped).strip()


def extract_author_from_anchors(anchors: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for anchor in anchors[:20]:
        text = normalize_text(anchor.get("text") or anchor.get("aria") or "")
        href = normalize_facebook_href(anchor.get("href"))
        if not text or not href:
            continue
        if href.startswith("https://www.facebook.com/groups/"):
            continue
        if not looks_like_author(text):
            continue
        return text, extract_author_id(href)
    return None, None


def parse_feed_block(block: dict[str, Any], group_url: str, min_content_length: int, keyword_filter: bool) -> dict[str, Any] | None:
    text = normalize_text(block.get("text"))
    message_text = normalize_text(block.get("message_text"))
    if not text:
        return None
    header_lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    content_source = message_text if len(message_text) >= 20 else text
    content_lines = [normalize_text(line) for line in content_source.splitlines() if normalize_text(line)]
    if not header_lines or not content_lines:
        return None

    anchor_candidates = block.get("anchors", [])
    author, author_id = extract_author_from_anchors(anchor_candidates)
    datetime_text = None
    if not author:
        for index, line in enumerate(header_lines[:40]):
            if looks_like_author(line):
                author = line
                for probe in header_lines[index : index + 12]:
                    extracted = extract_datetime_from_text(probe)
                    if extracted:
                        datetime_text = extracted
                        break
                if author:
                    break

    if not author:
        return None
    if not datetime_text:
        author_index = 0
        for index, line in enumerate(header_lines):
            if line == author:
                author_index = index
                break
        for probe in header_lines[author_index : author_index + 12]:
            extracted = extract_datetime_from_text(probe)
            if extracted:
                datetime_text = extracted
                break
    content = clean_content_lines(content_lines, author=author, datetime_text=datetime_text)
    if len(content) < min_content_length:
        return None
    if keyword_filter and not content_matches_keywords(content):
        return None

    post_url = None
    candidate_post_hrefs: list[str] = []
    href_candidates = [anchor.get("href") for anchor in anchor_candidates if anchor.get("href")]
    for href in href_candidates:
        normalized_href = normalize_facebook_href(href)
        if not author_id:
            author_id = extract_author_id(normalized_href)
        if not post_url:
            post_url = normalize_post_url(normalized_href)
        if looks_like_candidate_post_href(href):
            candidate_post_hrefs.append(normalized_href)
    if not datetime_text:
        for anchor in anchor_candidates:
            extracted = extract_datetime_from_text(anchor.get("text") or anchor.get("aria"))
            if extracted:
                datetime_text = extracted
                break
    if not post_url:
        for anchor in anchor_candidates:
            anchor_text = normalize_text(anchor.get("text") or anchor.get("aria"))
            href = anchor.get("href")
            if not href:
                continue
            if datetime_text and datetime_text in anchor_text:
                normalized_href = href if href.startswith("http") else f"https://www.facebook.com{href}"
                maybe_url = normalize_post_url(normalized_href)
                if maybe_url:
                    post_url = maybe_url
                    break
    if not post_url and candidate_post_hrefs:
        post_url = candidate_post_hrefs[0]

    return {
        "author": author,
        "author_id": author_id,
        "datetime": datetime_text,
        "content": content,
        "post_url": post_url,
        "candidate_post_hrefs": candidate_post_hrefs,
        "group_url": group_url,
        "images": block.get("images", []),
    }


def collect_feed_blocks(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(
        r"""
        () => {
          const feed = document.querySelector('[role="feed"]');
          if (!feed) return [];
          function normalizeText(value) {
            return (value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
          }
          function extractMessageText(node) {
            const messageNode =
              node.querySelector("div[data-ad-preview='message']") ||
              node.querySelector("div[data-ad-rendering-role='story_message']") ||
              node.querySelector("div[data-ad-comet-preview='message']");
            return messageNode ? normalizeText(messageNode.innerText || '') : '';
          }
          const candidates = [];
          const seen = new Set();
          const nodes = Array.from(feed.querySelectorAll(':scope > div, :scope > [data-pagelet], :scope > [role="article"], div[role="article"]'));
          for (const node of nodes) {
            const text = normalizeText(node.innerText || '');
            if (!text || text.length < 80) continue;
            if (!text.includes('Thích') || !text.includes('Bình luận')) continue;
            if (seen.has(text)) continue;
            seen.add(text);
            const anchors = Array.from(node.querySelectorAll('a[href]')).map(a => ({
              href: a.getAttribute('href'),
              text: normalizeText(a.innerText || ''),
              aria: a.getAttribute('aria-label')
            })).filter(item => item.href);
            const images = Array.from(node.querySelectorAll('img[src]'))
              .map(img => img.getAttribute('src'))
              .filter(Boolean)
              .filter(src => !src.includes('emoji') && !src.includes('static.xx.fbcdn.net'))
              .filter(src => !src.startsWith('data:image/svg+xml'));
            candidates.push({ text, message_text: extractMessageText(node), anchors, images });
          }
          return candidates;
        }
        """
    )


def click_see_more(page: Page, expand_rounds: int, pause_ms: int) -> int:
    total_clicked = 0
    for _ in range(expand_rounds):
        clicked = int(
            page.evaluate(
                """
                () => {
                  const labels = ['Xem thêm', 'Ẩn bớt'];
                  const nodes = Array.from(document.querySelectorAll('div, span, a'));
                  let count = 0;
                  for (const node of nodes) {
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text !== 'Xem thêm') continue;
                    const clickable = node.closest('[role="button"], a, [tabindex=\"0\"]') || node;
                    if (!(clickable instanceof HTMLElement)) continue;
                    clickable.click();
                    count += 1;
                  }
                  return count;
                }
                """
            )
        )
        total_clicked += clicked
        if not clicked:
            break
        page.wait_for_timeout(pause_ms)
    return total_clicked


def wait_for_feed_ready(page: Page, timeout_ms: int = 90000) -> None:
    deadline = datetime.now(timezone.utc).timestamp() + (timeout_ms / 1000)
    while datetime.now(timezone.utc).timestamp() < deadline:
        try:
            page.wait_for_selector(FEED_SELECTOR, timeout=5000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1500)
            continue
        text = normalize_text(page.evaluate("() => { const f = document.querySelector('[role=\"feed\"]'); return f ? f.innerText : ''; }"))
        if len(text) > 500 and "Bình luận" in text and "Thích" in text:
            logging.info("Desktop feed da san sang | text_len=%s", len(text))
            return
        page.wait_for_timeout(1500)
    raise PlaywrightTimeoutError("Desktop feed khong san sang trong thoi gian cho.")


def dedupe_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for post in posts:
        key = post.get("post_url") or f"{post.get('author','')}|{post.get('datetime','')}|{post.get('content','')[:200]}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(post)
    return deduped


def resolve_post_url(page: Page, candidate_hrefs: list[str]) -> str | None:
    for href in candidate_hrefs:
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            maybe = normalize_post_url(page.url)
            if maybe:
                return maybe
        except Exception:
            continue
    return None


def collect_page_artifacts(page: Page, output_dir: Path, slug: str) -> tuple[Path, Path]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    html_path = output_dir / f"{slug}_{timestamp}.html"
    png_path = output_dir / f"{slug}_{timestamp}.png"
    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=True)
    return html_path, png_path


def crawl_group(
    conn,
    page: Page,
    context_page_factory,
    group_url: str,
    output_dir: Path | None,
    crawl_run_id: int,
    scroll_rounds: int,
    scroll_px: int,
    scroll_pause_ms: int,
    expand_rounds: int,
    max_posts_per_group: int,
    min_content_length: int,
    keyword_filter: bool,
    near_duplicate_threshold: float,
) -> tuple[list[dict[str, Any]], Path | None, Path | None, int]:
    logging.info("Dang mo group: %s", group_url)
    page.goto(group_url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(7000)
    wait_for_feed_ready(page)

    collected: list[dict[str, Any]] = []
    run_seen: set[str] = set()
    stalled_rounds = 0
    last_block_count = 0
    total_inserted: int = 0

    for round_index in range(scroll_rounds):
        expanded = click_see_more(page, expand_rounds=expand_rounds, pause_ms=700)
        blocks = collect_feed_blocks(page)
        parsed_count = 0
        inserted_count = 0
        for block in blocks:
            post = parse_feed_block(
                block,
                group_url=group_url,
                min_content_length=min_content_length,
                keyword_filter=keyword_filter,
            )
            if not post:
                continue
            parsed_count += 1
            key = post.get("post_url") or f"{post.get('author','')}|{post.get('datetime','')}|{post.get('content','')[:200]}"
            if key in run_seen:
                continue
            run_seen.add(key)
            db_result = ingest_post(
                conn,
                crawl_run_id=crawl_run_id,
                post=post,
                near_duplicate_threshold=near_duplicate_threshold,
            )
            post["canonical_post_id"] = db_result["canonical_post_id"]
            post["post_observation_id"] = db_result["observation_id"]
            post["dedupe_method"] = db_result["dedupe_method"]
            post["dedupe_score"] = db_result["dedupe_score"]
            collected.append(post)
            if db_result["inserted"]:
                inserted_count += 1
            if len(collected) >= max_posts_per_group:
                break
        logging.info(
            "Round %s/%s | blocks=%s | parsed=%s | inserted=%s | expanded=%s",
            round_index + 1,
            scroll_rounds,
            len(blocks),
            parsed_count,
            inserted_count,
            expanded,
        )
        total_inserted += inserted_count
        if len(collected) >= max_posts_per_group:
            logging.info("Dung group som vi da dat max_posts_per_group=%s", max_posts_per_group)
            break
        page.evaluate(f"window.scrollBy(0, {int(scroll_px)});")
        page.wait_for_timeout(scroll_pause_ms)
        if len(blocks) <= last_block_count:
            stalled_rounds += 1
        else:
            stalled_rounds = 0
        last_block_count = max(last_block_count, len(blocks))
        if stalled_rounds >= 5:
            logging.info("Dung scroll som vi so block khong tang them.")
            break

    deduped = dedupe_posts(collected)
    resolver_page = context_page_factory()
    try:
        for post in deduped:
            if post.get("post_url"):
                continue
            candidate_hrefs = post.get("candidate_post_hrefs") or []
            if not candidate_hrefs:
                continue
            resolved = resolve_post_url(resolver_page, candidate_hrefs[:2])
            if resolved:
                post["post_url"] = resolved
    finally:
        resolver_page.close()
    html_path = None
    png_path = None
    if output_dir is not None:
        html_path, png_path = collect_page_artifacts(page, output_dir=output_dir, slug=safe_slug(group_url))
    return deduped, html_path, png_path, total_inserted


def enrich_posts(posts: list[dict[str, Any]], crawled_at: str, start_index: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset, post in enumerate(posts):
        rows.append(
            {
                "author": post.get("author"),
                "author_id": post.get("author_id"),
                "datetime": post.get("datetime"),
                "content": post.get("content"),
                "post_url": post.get("post_url"),
                "group_url": post.get("group_url"),
                "images": post.get("images", []),
                "canonical_post_id": post.get("canonical_post_id"),
                "post_observation_id": post.get("post_observation_id"),
                "dedupe_method": post.get("dedupe_method"),
                "dedupe_score": post.get("dedupe_score"),
                "crawled_at": crawled_at,
                "index": start_index + offset,
            }
        )
    return rows


def _crawl_single_group(
    args,
    group_url: str,
    profile_dir: Path,
    cookies: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Crawl one group in its own thread with its own Playwright context.
    Each thread gets its own browser profile subdirectory to avoid cookie/lock conflicts.
    """
    thread_name = threading.current_thread().name
    thread_profile_dir = profile_dir.parent / f"{profile_dir.name}_thread_{thread_name.replace(' ', '_')}"
    thread_profile_dir.mkdir(exist_ok=True)

    result: dict[str, Any] = {
        "group_url": group_url,
        "posts": [],
        "html_path": None,
        "png_path": None,
        "error": None,
    }

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(thread_profile_dir),
            headless=args.headless,
            viewport={"width": 1440, "height": 2200},
            locale="vi-VN",
            extra_http_headers={"Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        try:
            context.add_cookies(cookies)
            page = context.new_page()

            # Each thread creates its own DB connection
            database_url = get_database_url(args.database_url)
            with connect_db(database_url) as conn:
                ensure_schema(conn)
                crawl_run_id = create_crawl_run(conn, group_url)
                try:
                    posts, html_path, png_path, inserted = crawl_group(
                        conn,
                        page,
                        context.new_page,
                        group_url=group_url,
                        output_dir=args.output_dir,
                        crawl_run_id=crawl_run_id,
                        scroll_rounds=args.scroll_rounds,
                        scroll_px=args.scroll_px,
                        scroll_pause_ms=args.scroll_pause_ms,
                        expand_rounds=args.expand_rounds,
                        max_posts_per_group=args.max_posts_per_group,
                        min_content_length=args.min_content_length,
                        keyword_filter=args.keyword_filter,
                        near_duplicate_threshold=args.near_duplicate_threshold,
                    )
                    result["posts"] = posts
                    result["html_path"] = str(html_path)
                    result["png_path"] = str(png_path)
                    complete_crawl_run(
                        conn,
                        crawl_run_id,
                        "completed",
                        {"group_url": group_url, "posts_seen": len(posts), "posts_inserted": inserted},
                    )
                    # Update group stats
                    with conn.cursor() as cur:
                        cur.execute("SELECT id FROM fb_groups WHERE url = %s LIMIT 1", (group_url,))
                        row = cur.fetchone()
                    if row:
                        update_group_crawled(conn, row[0], inserted)
                    logging.info("[%s] Done: %s — %s posts", thread_name, group_url, len(posts))
                except PlaywrightTimeoutError:
                    complete_crawl_run(conn, crawl_run_id, "failed", {"error": "timeout", "group_url": group_url})
                    result["error"] = f"timeout: {group_url}"
                    logging.exception("[%s] Timeout: %s", thread_name, group_url)
                except Exception as exc:
                    result["error"] = f"{type(exc).__name__}: {exc}"
                    complete_crawl_run(conn, crawl_run_id, "failed", {"error": str(exc), "group_url": group_url})
                    logging.exception("[%s] Error: %s", thread_name, group_url)
                finally:
                    page.close()
        finally:
            context.close()

    return result


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.log_level)

    output_dir = Path(args.output_dir) if args.output_dir else None
    profile_dir = Path(args.profile_dir)
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    # Get cookies: DB (alive) or file
    cookies: list[dict[str, Any]] = []
    if args.use_db_cookies:
        db_url = get_database_url(args.database_url)
        with connect_db(db_url) as conn:
            ensure_schema(conn)
            cookie_json = get_live_cookie_json(conn)
            if cookie_json is None:
                logging.error("Khong co cookie alive trong DB. Chay /api/admin/cookies/{id}/validate truoc.")
                return 1
            # Transform DB cookie JSON into Playwright cookie format
            cookies = _transform_db_cookies(cookie_json)
            logging.info("Lay %s cookies tu DB (alive)", len(cookies))
    else:
        cookies_file = Path(args.cookies_file)
        if not cookies_file.exists():
            logging.error("Khong tim thay cookies file: %s", cookies_file)
            return 1
        cookies = load_facebook_cookies(cookies_file)
        logging.info("Lay %s cookies tu file: %s", len(cookies), cookies_file)

    # Get group URLs: DB (active) or file
    group_urls: list[str] = []
    if args.use_db_groups:
        db_url = get_database_url(args.database_url)
        with connect_db(db_url) as conn:
            ensure_schema(conn)
            active_groups = get_active_groups(conn)
            group_urls = [g["url"] for g in active_groups]
            logging.info("Lay %s groups active tu DB", len(group_urls))
            if args.max_groups:
                group_urls = group_urls[: args.max_groups]
    else:
        if args.group_url:
            group_urls = [args.group_url]
        else:
            groups_file = Path(args.groups_file)
            group_urls = load_group_urls(groups_file, args.max_groups)

    if not group_urls:
        logging.error("Khong co group nao de crawl.")
        return 1

    crawled_at = datetime.now(timezone.utc).isoformat()

    # Attach optional output_dir as attribute so worker threads can access it
    args.output_dir = output_dir

    all_rows: list[dict[str, Any]] = []
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_crawl_single_group, args, url, profile_dir, cookies): url for url in group_urls}

        for future in as_completed(futures):
            group_url = futures[future]
            try:
                result = future.result()
                if result["error"]:
                    failed += 1
                    logging.error("[FAIL] %s — %s", group_url, result["error"])
                else:
                    completed += 1
                    posts = result["posts"]
                    remaining = args.max_posts - len(all_rows) if args.max_posts else len(posts)
                    all_rows.extend(enrich_posts(posts[:remaining] if remaining > 0 else [], crawled_at=crawled_at, start_index=len(all_rows) + 1))
                    if args.max_posts and len(all_rows) >= args.max_posts:
                        logging.info("Da dat gioi han %s bai — dung lai.", args.max_posts)
                        # Cancel remaining futures
                        for f in futures:
                            f.cancel()
                        break
            except Exception as exc:
                failed += 1
                logging.exception("[FATAL] Worker error for %s: %s", group_url, exc)

    if output_dir is not None:
        output_file = output_dir / f"bds_thanhhoa_desktop_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        output_file.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info(
            "Xong! Tong: %s bai (completed=%s, failed=%s) → %s",
            len(all_rows), completed, failed, output_file,
        )
    else:
        logging.info(
            "Xong! Tong: %s bai (completed=%s, failed=%s) — da luu truc tiep vao database.",
            len(all_rows), completed, failed,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
