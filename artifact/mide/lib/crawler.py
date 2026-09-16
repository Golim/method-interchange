import re
import signal
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable
from urllib.parse import urlparse, urldefrag, urlunparse

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Request, Error, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

from lib.config import Config
from lib.logging_setup import get_logger
from lib.stealth import (
    IGNORE_DEFAULT_ARGS,
    STEALTH_CONTEXT_OPTIONS,
    merged_browser_args,
)
from lib.utils import (
    normalize_url,
    is_excluded_extension,
    compute_request_hash,
    validate_url,
    extract_domain
)
from lib.actions_filter import match_dangerous_action, detect_file_upload

# Resource types for which POST requests are captured.
# All other types (ping, websocket, image, stylesheet, etc.) are filtered out
# as noise before writing to JSONL.
ALLOWED_POST_RESOURCE_TYPES = frozenset({'xhr', 'fetch', 'document', 'other'})


class CrawlerEngine:
    """
    Browser automation engine for crawling websites and capturing POST requests.

    Uses Playwright to launch a browser, navigate pages via BFS, intercept
    network requests, and extract links. Deduplicates URLs and POST requests
    based on configuration.

    Attributes:
        target_url: Starting URL for crawling
        target_domain: Domain extracted from target_url
        config: Configuration instance with crawling parameters
        intercepted_posts: List of captured POST request dictionaries
        seen_request_hashes: Set of request hashes for deduplication
        visited_urls: Set of visited URLs
        url_queue: BFS queue for URL crawling
        pages_crawled: Counter for pages visited
        on_post_captured: Callback function called when POST captured
        on_progress: Callback function called on crawl progress
    """

    def __init__(self, target_url: str, config: Config, storage_state: str | dict | None = None):
        """
        Initialize crawler engine.

        Args:
            target_url: URL to start crawling from
            config: Configuration instance with settings
            storage_state: Optional path to session JSON or dict with cookies/origins.
                           If provided, creates browser context with authenticated state.
        """
        self.target_url = target_url
        self.target_domain = extract_domain(target_url)
        self.config = config
        self.logger = get_logger("crawler")

        # Request capture state
        self.intercepted_post_count: int = 0
        self.filtered_post_count: int = 0
        self.seen_request_hashes: set = set()
        self._pending_post_captures: Dict[int, Dict[str, Any]] = {}

        # Crawling state
        self.visited_urls: set = set()
        self.url_queue: deque = deque()
        self.url_depths: Dict[str, int] = {}
        self.pages_crawled: int = 0

        # Control flags
        self._shutdown_requested: bool = False

        # Browser state
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

        # Callback hooks
        self.on_post_captured: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_progress: Optional[Callable[..., None]] = None

        # Storage state for authenticated crawling
        self._storage_state = storage_state

        # Pre-compile blacklist regex patterns (performance optimization)
        self._blacklist_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in self.config.blacklisted_url_patterns
        ]

    def clean_url(self, url: str) -> str:
        """
        Clean URL by stripping whitespace and newline characters.

        Args:
            url: URL to clean

        Returns:
            Cleaned URL string
        """
        return url.strip().strip('\n').strip('\r').strip('\t')

    def get_template_url(self, url: str, path: bool = True) -> str:
        """
        Create URL template by removing digits from path.

        Used for pattern-based deduplication to avoid re-visiting URLs
        that differ only by ID numbers (e.g., /post/1 and /post/2).

        Args:
            url: URL to convert to template
            path: If True, process full path; if False, exclude last path segment

        Returns:
            Template URL with digits removed from path
        """
        parsed = urlparse(urldefrag(url)[0])
        if path:
            template_url = urlunparse(('', parsed.netloc, re.sub(r'\d+', '', parsed.path), '', '', ''))
            template_url = template_url.lstrip("/").rstrip("/")
            return template_url
        else:
            if len(parsed.path.split('/')) > 1:
                _path = parsed.path.replace(parsed.path.split('/')[-1], '')
            else:
                _path = parsed.path
            return urlunparse(('', parsed.netloc, re.sub(r'\d+', '', _path), '', '', ''))

    def is_internal_url(self, url: str) -> bool:
        """
        Check if URL belongs to site (subdomains considered internal).

        Args:
            url: URL to check

        Returns:
            True if URL is internal to site, False otherwise
        """
        try:
            if not url.startswith('http'):
                url = 'http://' + url
            parsed = urlparse(url)
            host = parsed.netloc.lower()

            # Exact match or subdomain match (must have dot before domain)
            return host == self.target_domain or host.endswith('.' + self.target_domain)
        except Exception:
            return False

    def is_blacklisted(self, url: str) -> bool:
        """
        Check if URL should be blacklisted (analytics, tracking, logout).

        Checks URL against blacklisted domains and URL patterns from config.
        Used to filter out noise from analytics beacons and tracking requests.

        Args:
            url: URL to check

        Returns:
            True if URL matches blacklist, False otherwise
        """
        parsed = urlparse(url)

        # Check blacklisted domains
        for domain in self.config.blacklisted_domains:
            if domain in parsed.netloc:
                self.logger.debug("URL blacklisted (domain): %s (matches %s)", url, domain)
                return True

        # Check blacklisted URL patterns
        for pattern in self._blacklist_patterns:
            if pattern.search(url):
                self.logger.debug("URL blacklisted (pattern): %s (matches %s)", url, pattern.pattern)
                return True

        return False

    def start(self) -> None:
        """
        Initialize Playwright and launch browser.

        Creates sync_playwright instance, launches chromium browser,
        and creates browser context for cookie isolation.
        """
        self._playwright = sync_playwright().start()
        launch_args = merged_browser_args(self.config.browser_args)
        self._browser = self._playwright.chromium.launch(
            headless=self.config.headless,
            args=launch_args,
            ignore_default_args=list(IGNORE_DEFAULT_ARGS),
        )
        if self._storage_state is not None:
            self._context = self._browser.new_context(
                storage_state=self._storage_state,
                **STEALTH_CONTEXT_OPTIONS,
            )
        else:
            self._context = self._browser.new_context(**STEALTH_CONTEXT_OPTIONS)

    def stop(self) -> None:
        """
        Clean up browser resources.

        Closes browser context, browser, and stops Playwright.
        Handles cleanup errors gracefully.
        """
        try:
            if self._context:
                self._run_browser_call(5000, self._context.close)
        except Exception:
            pass

        try:
            if self._browser:
                self._run_browser_call(5000, self._browser.close)
        except Exception:
            pass

        try:
            if self._playwright:
                self._run_browser_call(5000, self._playwright.stop)
        except Exception:
            pass

    def _run_browser_call(self, timeout_ms: int, func: Callable, *args, **kwargs):
        """Run a sync Playwright call with a last-resort wall-clock alarm."""
        try:
            previous_handler = signal.getsignal(signal.SIGALRM)
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
        except (AttributeError, ValueError):
            return func(*args, **kwargs)

        def timeout_handler(signum, frame):
            raise TimeoutError(f"browser call exceeded {timeout_ms}ms")

        try:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, max(timeout_ms, 1) / 1000)
            return func(*args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    def _handle_request_started(self, request: Request) -> None:
        """
        Handle observed network requests.

        Captures POST requests to target domain and deduplicates by hash.
        This is deliberately passive: no route interception, no fetch, no
        fulfill, and no continue call. Response bodies are captured later from
        Playwright's requestfinished/requestfailed events.

        Args:
            request: Playwright request object
        """
        try:
            if request.method == "POST" and self.is_internal_url(request.url) and not self.is_blacklisted(request.url):
                # Filter out noise resource types (ping, websocket, image, etc.)
                if request.resource_type not in ALLOWED_POST_RESOURCE_TYPES:
                    self.filtered_post_count += 1
                    return

                # ---- actions filter dispatch ----
                # Drop dangerous-action POSTs and file-upload POSTs at capture-time.
                matched_pattern = match_dangerous_action(request.url, self.config.dangerous_action_patterns)
                if matched_pattern is not None:
                    self._record_actions_skip(
                        url=request.url,
                        method=request.method,
                        reason="dangerous_action",
                        matched_pattern=matched_pattern,
                        upload_signal=None,
                    )
                    return

                # Must use post_data_buffer (bytes), not post_data (str).
                upload_signal = detect_file_upload(request.headers, request.post_data_buffer)
                if upload_signal is not None:
                    self._record_actions_skip(
                        url=request.url,
                        method=request.method,
                        reason="file_upload",
                        matched_pattern=None,
                        upload_signal=upload_signal,
                    )
                    return
                # ---- end actions filter dispatch ----

                # Extract post data
                post_data = request.post_data
                if isinstance(post_data, bytes):
                    post_data = post_data.decode('utf-8', errors='replace')

                # Build request data dictionary
                request_data = {
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "postData": post_data,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "resourceType": request.resource_type,
                }

                # Deduplicate if enabled
                bypass_dedup = any(p.lower() in request.url.lower() for p in self.config.dedup_bypass_patterns)
                if self.config.deduplicate and not bypass_dedup:
                    # Extract content_type case-insensitively from request headers.
                    content_type = ""
                    for k, v in request.headers.items():
                        if k.lower() == "content-type":
                            content_type = v
                            break
                    request_hash = compute_request_hash(request.url, content_type, post_data)
                    if request_hash not in self.seen_request_hashes:
                        self.seen_request_hashes.add(request_hash)
                        self._queue_post_capture(request, request_data)
                    else:
                        self.logger.debug("POST deduplicated: %s", request.url)
                else:
                    self._queue_post_capture(request, request_data)

        except Exception as e:
            self.logger.debug("Request capture error (non-fatal): %s", str(e))

    def _queue_post_capture(self, request: Request, request_data: Dict[str, Any]) -> None:
        """Store POST metadata until Playwright reports the request completed."""
        self.intercepted_post_count += 1
        self._pending_post_captures[id(request)] = request_data
        self.logger.info("POST intercepted: %s (%s)", request.url, request.resource_type)

        if self.config.max_posts > 0 and self.intercepted_post_count >= self.config.max_posts:
            self.request_shutdown()

    def _handle_request_finished(self, request: Request) -> None:
        """Attach response data to an intercepted POST after the browser finishes it."""
        request_data = self._pending_post_captures.pop(id(request), None)
        if request_data is None:
            return

        try:
            response = request.response()
            if response is None:
                request_data["response"] = self._make_capture_failure_response(
                    "missing_response",
                    "Playwright reported requestfinished without a response object",
                )
            else:
                headers = dict(response.headers)
                location = headers.get("location")
                try:
                    body = self._run_browser_call(
                        self.config.form_action_timeout,
                        response.text,
                    )
                except Exception as body_exc:
                    body = ""
                    headers["x-crawler-body-capture-error"] = str(body_exc)
                request_data["response"] = {
                    "status_code": response.status,
                    "headers": headers,
                    "body": body,
                    "redirect_chain": (
                        [{"location": location, "status": response.status}]
                        if location is not None
                        else []
                    ),
                }
        except Exception as exc:
            request_data["response"] = self._make_capture_failure_response(
                "response_capture_error",
                str(exc),
            )

        self._emit_captured_post(request_data)

    def _handle_request_failed(self, request: Request) -> None:
        """Persist intercepted POSTs that fail before a response is available."""
        request_data = self._pending_post_captures.pop(id(request), None)
        if request_data is None:
            return

        failure = request.failure or "unknown request failure"
        request_data["response"] = self._make_capture_failure_response(
            "request_failed",
            failure,
        )
        self._emit_captured_post(request_data)

    def _flush_pending_post_captures(self, reason: str) -> None:
        """Persist any POSTs that were seen but had no completion event yet."""
        pending = list(self._pending_post_captures.values())
        self._pending_post_captures.clear()
        for request_data in pending:
            request_data["response"] = self._make_capture_failure_response(
                reason,
                "Crawl ended before Playwright emitted requestfinished/requestfailed",
            )
            self._emit_captured_post(request_data)

    def _make_capture_failure_response(self, failure_type: str, message: str) -> Dict[str, Any]:
        return {
            "status_code": None,
            "headers": {},
            "body": "",
            "redirect_chain": [],
            "failure": {
                "failure_type": failure_type,
                "failure_message": message,
            },
        }

    def _emit_captured_post(self, request_data: Dict[str, Any]) -> None:
        if self.on_post_captured:
            try:
                self.on_post_captured(request_data)
            except IOError:
                self.request_shutdown()

    def _record_actions_skip(
        self,
        *,
        url: str,
        method: str,
        reason: str,           # "dangerous_action" | "file_upload"
        matched_pattern: Optional[str],
        upload_signal: Optional[str],
    ) -> None:
        """Emit one WARNING per dropped request.
        """
        pattern = matched_pattern if reason == "dangerous_action" else upload_signal
        self.logger.warning(
            "actions-filter: skipped %s %s reason=%s pattern=%s",
            method,
            url,
            reason,
            pattern,
        )

    def _navigate_page(self, page: Page, url: str) -> bool:
        """
        Navigate to URL with timeout handling.

        Args:
            page: Playwright page object
            url: URL to navigate to

        Returns:
            True if navigation succeeded, False on error
        """
        try:
            self._run_browser_call(
                self.config.page_load_timeout + 5000,
                page.goto,
                url,
                wait_until="domcontentloaded",
                timeout=self.config.page_load_timeout,
            )
            # Brief settle time for JS-triggered requests
            self._run_browser_call(3000, page.wait_for_timeout, 2000)
            # Track template URL to deduplicate pattern-based URLs
            template_url = self.get_template_url(url)
            self.visited_urls.add(template_url)
            return True
        except TimeoutError as e:
            self.logger.warning(
                "Navigation watchdog timeout: %s - %s; stopping current site",
                url,
                str(e),
            )
            self.request_shutdown()
            return False
        except PlaywrightTimeoutError:
            self.logger.warning("Navigation timeout: %s", url)
            return False
        except Error as e:
            self.logger.error("Navigation error: %s - %s", url, str(e))
            return False

    def _extract_links(self, page: Page) -> List[str]:
        """
        Extract links from current page.

        Filters links by target domain, excluded extensions,
        and visited status. Resolves relative URLs to absolute.
        Respects only_internal config for link filtering.

        Args:
            page: Playwright page object

        Returns:
            List of valid unvisited URLs
        """
        try:
            hrefs = self._run_browser_call(
                self.config.form_action_timeout,
                page.evaluate,
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href'))
                    .filter(Boolean)
                """,
            )
            links = []

            for href in hrefs:
                try:
                    # Resolve relative URLs to absolute
                    absolute_url = normalize_url(href, page.url)

                    # Validate URL
                    if not validate_url(absolute_url):
                        continue

                    # Filter: check only_internal config
                    if self.config.only_internal:
                        # Only crawl internal links (subdomains considered internal)
                        if not self.is_internal_url(absolute_url):
                            continue

                    # Filter: exclude file extensions
                    if is_excluded_extension(absolute_url):
                        continue

                    # Filter: exclude blacklisted URLs (analytics, tracking, logout)
                    if self.is_blacklisted(absolute_url):
                        continue

                    # Filter: check if template URL already visited
                    template_url = self.get_template_url(absolute_url)
                    if template_url in self.visited_urls:
                        continue

                    links.append(absolute_url)

                except Exception:
                    # Skip invalid links
                    continue

            # Deduplicate links
            return list(set(links))

        except Exception:
            return []

    def _interact_with_forms(self, page: Page) -> None:
        """
        Interact with forms and buttons to trigger POST requests.

        This path avoids Playwright element locators. All page interaction is
        done with bounded DOM scripts plus a wall-clock watchdog, because some
        login pages can wedge individual locator operations indefinitely.

        Args:
            page: Playwright page object
        """
        original_url = page.url
        deadline = time.monotonic() + (self.config.form_interaction_timeout / 1000)
        action_timeout = self.config.form_action_timeout

        def budget_remaining() -> bool:
            return time.monotonic() < deadline and not self._shutdown_requested

        def restore_original_url(wait_ms: int = 500) -> None:
            try:
                if page.url != original_url:
                    self._run_browser_call(
                        min(self.config.page_load_timeout, action_timeout * 5) + 1000,
                        page.goto,
                        original_url,
                        wait_until="domcontentloaded",
                        timeout=min(self.config.page_load_timeout, action_timeout * 5),
                    )
                    self._run_browser_call(wait_ms + 1000, page.wait_for_timeout, wait_ms)
            except Exception:
                pass

        form_script = """
        ({ index, maxFields, targetDomain, blacklistPatterns }) => {
            const forms = Array.from(document.forms);
            const form = forms[index];
            if (!form) return { ok: false, reason: "missing_form", formCount: forms.length };

            const method = (form.getAttribute("method") || "get").toLowerCase();
            if (method !== "post") return { ok: false, reason: "non_post", formCount: forms.length };

            const action = form.getAttribute("action") || location.href;
            const actionUrl = new URL(action, location.href);
            const host = actionUrl.hostname.toLowerCase();
            const domain = String(targetDomain || "").toLowerCase();
            if (!(host === domain || host.endsWith("." + domain))) {
                return { ok: false, reason: "external_action", action: actionUrl.href, formCount: forms.length };
            }
            for (const pattern of blacklistPatterns || []) {
                if (new RegExp(pattern, "i").test(actionUrl.href)) {
                    return { ok: false, reason: "blacklisted_action", action: actionUrl.href, formCount: forms.length };
                }
            }

            const dummy = { email: "test@example.com", password: "Dead@123", number: "42", tel: "05550100" };
            const skip = new Set(["hidden", "submit", "button", "reset", "image", "file"]);
            const controls = Array.from(form.querySelectorAll("input, textarea, select")).slice(0, maxFields);
            const seenRadios = new Set();

            for (const el of controls) {
                el.removeAttribute("required");
                el.removeAttribute("pattern");
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute("type") || "text").toLowerCase();
                try {
                    if (tag === "select") {
                        const opt = Array.from(el.options).find(o => o.value !== "");
                        if (opt) el.value = opt.value;
                    } else if (type === "checkbox") {
                        el.checked = true;
                    } else if (type === "radio") {
                        const name = el.getAttribute("name") || "";
                        if (!seenRadios.has(name)) {
                            seenRadios.add(name);
                            el.checked = true;
                        }
                    } else if (!skip.has(type)) {
                        el.value = dummy[type] || "test";
                        el.dispatchEvent(new Event("input", { bubbles: true }));
                        el.dispatchEvent(new Event("change", { bubbles: true }));
                    }
                } catch (_) {}
            }

            const submitter = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
            if (submitter) {
                submitter.click();
            } else if (typeof form.requestSubmit === "function") {
                form.requestSubmit();
            } else {
                form.submit();
            }
            return { ok: true, reason: "submitted", action: actionUrl.href, formCount: forms.length };
        }
        """

        button_script = """
        ({ index, dangerousPatterns }) => {
            const buttons = Array.from(document.querySelectorAll(
                "button:not(form button), [role=button]:not(form [role=button]), input[type=submit]:not(form input[type=submit])"
            ));
            const button = buttons[index];
            if (!button) return { ok: false, reason: "missing_button", buttonCount: buttons.length };
            const text = (button.innerText || button.value || button.getAttribute("aria-label") || "").toLowerCase();
            for (const pattern of dangerousPatterns || []) {
                if (text.includes(String(pattern).toLowerCase())) {
                    return { ok: false, reason: "dangerous_text", buttonCount: buttons.length };
                }
            }
            button.click();
            return { ok: true, reason: "clicked", buttonCount: buttons.length };
        }
        """

        try:
            form_count = int(self._run_browser_call(action_timeout, page.evaluate, "() => document.forms.length") or 0)
            form_limit = min(form_count, self.config.max_forms)
            if form_count:
                self.logger.info(
                    "Form interaction: %d forms on %s (processing %d)",
                    form_count,
                    original_url,
                    form_limit,
                )

            for i in range(form_limit):
                if not budget_remaining():
                    self.logger.debug("Form interaction budget exhausted on %s", original_url)
                    return
                self.logger.info("Processing form %d/%d on %s", i + 1, form_limit, original_url)
                try:
                    self._run_browser_call(
                        action_timeout,
                        page.evaluate,
                        form_script,
                        {
                            "index": i,
                            "maxFields": self.config.max_form_fields,
                            "targetDomain": self.target_domain,
                            "blacklistPatterns": self.config.blacklisted_url_patterns,
                        },
                    )
                except Exception as e:
                    self.logger.debug("Form %d DOM interaction failed: %s", i + 1, str(e))
                settle_ms = min(1000, action_timeout)
                self._run_browser_call(settle_ms + 1000, page.wait_for_timeout, settle_ms)
                restore_original_url()
        except Exception as e:
            self.logger.debug("Form interaction error: %s", str(e))

        try:
            button_count = 0
            if self.config.max_clickable > 0:
                button_count = int(self._run_browser_call(
                    action_timeout,
                    page.evaluate,
                    """() => document.querySelectorAll(
                        "button:not(form button), [role=button]:not(form [role=button]), input[type=submit]:not(form input[type=submit])"
                    ).length""",
                ) or 0)
                button_count = min(button_count, self.config.max_clickable)

            for i in range(button_count):
                if not budget_remaining():
                    self.logger.debug("Button interaction budget exhausted on %s", original_url)
                    return
                try:
                    self._run_browser_call(
                        action_timeout,
                        page.evaluate,
                        button_script,
                        {
                            "index": i,
                            "dangerousPatterns": self.config.dangerous_action_patterns,
                        },
                    )
                except Exception as e:
                    self.logger.debug("Button %d DOM interaction failed: %s", i + 1, str(e))
                settle_ms = min(2000, action_timeout)
                self._run_browser_call(settle_ms + 1000, page.wait_for_timeout, settle_ms)
                restore_original_url()
        except Exception as e:
            self.logger.debug("Button interaction error: %s", str(e))

        restore_original_url(wait_ms=min(2000, action_timeout))
        self.logger.info("Form interaction complete on %s", original_url)

    def request_shutdown(self) -> None:
        """
        Request graceful shutdown of crawler.

        Sets shutdown flag which will stop crawl loop after current page.
        """
        self._shutdown_requested = True
        self.logger.info("Shutdown requested, saving %d captured requests", self.intercepted_post_count)

    def crawl(self) -> List[Dict[str, Any]]:
        """
        Main crawling entry point.

        Performs BFS crawling starting from target_url, intercepts POST
        requests, and respects max_pages/max_depth limits.

        Returns:
            List of intercepted POST request dictionaries
        """
        self.logger.info("Starting crawl of %s (max_pages=%d, max_depth=%d, max_posts=%d)", self.target_url, self.config.max_pages, self.config.max_depth, self.config.max_posts)

        try:
            # Initialize browser
            self.start()

            # Create page and attach passive network handlers.
            page = self._context.new_page()
            page.set_default_timeout(self.config.form_action_timeout)
            page.set_default_navigation_timeout(self.config.page_load_timeout)
            Stealth().apply_stealth_sync(page)
            page.on("request", self._handle_request_started)
            page.on("requestfinished", self._handle_request_finished)
            page.on("requestfailed", self._handle_request_failed)

            # Seed queue with target URL
            self.url_queue.append(self.target_url)
            self.url_depths[self.target_url] = 0

            # BFS crawling loop
            while self.url_queue and not self._shutdown_requested:
                # Check max_pages limit
                if self.pages_crawled >= self.config.max_pages:
                    break

                # Get next URL from queue
                url = self.url_queue.popleft()

                # Skip if already visited (check template URL for pattern deduplication)
                template_url = self.get_template_url(url)
                if template_url in self.visited_urls:
                    continue

                # Check depth limit
                depth = self.url_depths.get(url, 0)
                if depth > self.config.max_depth:
                    continue

                # Navigate to URL
                self.logger.info("Navigating to %s (depth=%d)", url, depth)
                success = self._navigate_page(page, url)

                if success:
                    self.pages_crawled += 1

                    # Extract links from page
                    new_links = self._extract_links(page)
                    self.logger.debug("Page loaded: %s (%d links found)", url, len(new_links))

                    # Add new links to queue with depth tracking
                    for link in new_links:
                        if link not in self.visited_urls and link not in self.url_depths:
                            self.url_queue.append(link)
                            self.url_depths[link] = depth + 1

                    # Interact with forms and buttons
                    self._interact_with_forms(page)

                    # Invoke progress callback
                    if self.on_progress:
                        self.on_progress(
                            pages=self.pages_crawled,
                            posts=self.intercepted_post_count,
                            depth=depth,
                            url=url
                        )

                    # Sleep between pages if configured
                    if self.config.sleep_between_pages > 0:
                        self._run_browser_call(
                            self.config.sleep_between_pages + 1000,
                            page.wait_for_timeout,
                            self.config.sleep_between_pages,
                        )

            self._flush_pending_post_captures("crawl_ended")

            # Close page
            self._run_browser_call(5000, page.close)

        except Exception as e:
            # Crawl error - return what we collected
            self.logger.error("Crawl error: %s", str(e))
        finally:
            # Always clean up browser resources
            self.stop()

        self.logger.info("Crawl complete: %d pages, %d POST requests, %d filtered", self.pages_crawled, self.intercepted_post_count, self.filtered_post_count)
        return self.intercepted_post_count
