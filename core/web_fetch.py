#!/usr/bin/env python3
"""
Evo IDE V5 - Web Fetch Module

Real-time web scraping and API fetching for the agent.
"""
import json
import time
import urllib.request
import urllib.error
import re
from typing import Optional, Dict, Any
from html.parser import HTMLParser

from .config import logger
from .state import web_cache, web_cache_lock, WEB_CACHE_MAX_AGE


class HTMLStripper(HTMLParser):
    """Strip HTML tags to extract plain text."""

    def __init__(self):
        super().__init__()
        self.reset()
        self.text = []
        self.skip_tags = {'script', 'style', 'nav', 'header', 'footer', 'aside'}
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.text.append(data)

    def get_text(self):
        return ' '.join(self.text)


def strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    stripper = HTMLStripper()
    try:
        stripper.feed(html)
        text = stripper.get_text()
        text = re.sub(r'[ ' + chr(9) + ']+', ' ', text)
        text = re.sub(chr(10) + chr(9) + '*', chr(10), text)
        return text.strip()[:8000]
    except Exception as e:
        logger.warning(f"HTML stripping failed: {e}")
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'[ ' + chr(9) + ']+', ' ', text)
        return text.strip()[:8000]


def fetch_url(url: str, timeout: int = 15, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Fetch content from a URL with caching."""
    cache_key = url
    with web_cache_lock:
        if cache_key in web_cache:
            entry = web_cache[cache_key]
            age = time.time() - entry["timestamp"]
            if age < WEB_CACHE_MAX_AGE:
                logger.info(f"Cache hit for {url} (age={age:.0f}s)")
                return {**entry, "cached": True}

    default_headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }
    if headers:
        default_headers.update(headers)

    result = {
        "success": False,
        "url": url,
        "content": "",
        "content_type": "",
        "status_code": 0,
        "cached": False,
        "error": None,
        "timestamp": time.time()
    }

    try:
        req = urllib.request.Request(url, headers=default_headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result["status_code"] = response.getcode()
            content_type = response.headers.get('Content-Type', '')
            result["content_type"] = content_type

            raw = response.read()

            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = raw.decode('latin-1')
                except:
                    text = raw.decode('utf-8', errors='ignore')

            if 'application/json' in content_type:
                try:
                    data = json.loads(text)
                    result["content"] = json.dumps(data, indent=2)
                except json.JSONDecodeError:
                    result["content"] = text[:8000]
            elif 'text/html' in content_type:
                result["content"] = strip_html(text)[:8000]
            else:
                result["content"] = text[:8000]

            result["success"] = True

    except urllib.error.HTTPError as e:
        result["status_code"] = e.code
        result["error"] = f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        result["error"] = f"URL Error: {e.reason}"
    except Exception as e:
        result["error"] = f"Fetch error: {str(e)}"

    if result["success"]:
        with web_cache_lock:
            web_cache[cache_key] = {
                "success": result["success"],
                "url": result["url"],
                "content": result["content"],
                "content_type": result["content_type"],
                "status_code": result["status_code"],
                "cached": False,
                "error": result["error"],
                "timestamp": result["timestamp"]
            }
            if len(web_cache) > 100:
                oldest = min(web_cache.keys(), key=lambda k: web_cache[k]["timestamp"])
                del web_cache[oldest]

    return result


def fetch_api(url: str, method: str = "GET", data: Optional[Dict] = None, 
              timeout: int = 15) -> Dict[str, Any]:
    """Fetch from a JSON API endpoint."""
    headers = {
        'User-Agent': 'Evo-IDE-Agent/5.0',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }

    if data and method.upper() == "POST":
        req_data = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers, method=method.upper())

    result = {
        "success": False,
        "url": url,
        "data": None,
        "status_code": 0,
        "error": None
    }

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result["status_code"] = response.getcode()
            text = response.read().decode('utf-8')
            try:
                result["data"] = json.loads(text)
                result["success"] = True
            except json.JSONDecodeError:
                result["error"] = "Invalid JSON response"
                result["data"] = text[:4000]
    except Exception as e:
        result["error"] = str(e)

    return result


def get_web_context(urls: list, max_chars: int = 4000) -> str:
    """Fetch multiple URLs and format as context for the LLM."""
    contexts = []
    for url in urls:
        result = fetch_url(url)
        if result["success"]:
            content = result["content"][:max_chars]
            contexts.append("--- Content from " + url + " ---\n" + content + "\n")
        else:
            contexts.append("--- Failed to fetch " + url + ": " + result['error'] + " ---\n")

    return "\n".join(contexts)


def clear_web_cache() -> int:
    """Clear the web cache. Returns number of entries removed."""
    with web_cache_lock:
        count = len(web_cache)
        web_cache.clear()
        return count
