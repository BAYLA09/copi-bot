from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

from models import Position, PositionStore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
DEFAULT_COPY_URL = "https://ct.fiper.me/copy/copyingAccount/9093547-fiper-demo"

COPY_URL = os.getenv("COPY_URL", DEFAULT_COPY_URL).strip()
CTRADER_EMAIL = os.getenv("CTRADER_EMAIL", "").strip()
CTRADER_PASSWORD = os.getenv("CTRADER_PASSWORD", "").strip()
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes"}
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))
SCREENSHOTS_DIR = Path(os.getenv("SCREENSHOTS_DIR", "screenshots"))

POSITIONS_TABLE_SELECTORS = [
    ".ag-root",
    ".ag-center-cols-container",
    "[role='grid']",
    "table:has(thead)",
]

POSITIONS_HEADER_SELECTORS = [
    "text=Symbol",
    "text=Volume",
    "text=Entry price",
    "text=Open price",
    "text=Direction",
]

POSITIONS_TAB_SELECTORS = [
    "role=tab[name=/Positions/i]",
    "button:has-text('Positions')",
    "a:has-text('Positions')",
    "[data-testid*='positions' i]",
    "text=Positions",
]

EXTRACT_POSITIONS_JS = """
() => {
  const normalize = (text) => (text || '').trim().replace(/\\s+/g, ' ');

  const getHeaders = () => {
    const selectors = [
      '.ag-header-cell-text',
      '.ag-header-cell .ag-header-cell-label',
      'thead th',
      '[role="columnheader"]',
    ];

    for (const selector of selectors) {
      const headers = Array.from(document.querySelectorAll(selector))
        .map((header) => normalize(header.textContent))
        .filter(Boolean);
      if (headers.length > 2) {
        return headers;
      }
    }
    return [];
  };

  const headers = getHeaders();
  const headerMap = {};

  const assignIndex = (keys, index) => {
    for (const key of keys) {
      headerMap[key] = index;
    }
  };

  headers.forEach((header, index) => {
    const key = header.toLowerCase();
    headerMap[key] = index;

    if (key.includes('symbol')) assignIndex(['symbol'], index);
    if (key.includes('direction') || key === 'side' || key === 'type') {
      assignIndex(['side'], index);
    }
    if (
      key.includes('entry') ||
      key === 'open price' ||
      (key.includes('open') && key.includes('price'))
    ) {
      assignIndex(['entry_price'], index);
    }
    if (
      key.includes('current') ||
      key === 'market price' ||
      (key === 'price' && headerMap.entry_price === undefined)
    ) {
      assignIndex(['current_price'], index);
    }
    if (key.includes('volume') || key.includes('qty') || key.includes('size')) {
      assignIndex(['volume'], index);
    }
    if (
      key.includes('stop') ||
      key === 'sl' ||
      key === 's/l' ||
      key === 's.l.'
    ) {
      assignIndex(['sl'], index);
    }
    if (
      key.includes('take') ||
      key === 'tp' ||
      key === 't/p' ||
      key === 't.p.'
    ) {
      assignIndex(['tp'], index);
    }
    if (key.includes('id') || key === '#') {
      assignIndex(['position_id'], index);
    }
  });

  const rowSelectors = [
    '.ag-center-cols-container .ag-row',
    '.ag-row',
    'tbody tr',
    '[role="row"]',
  ];

  let rows = [];
  for (const selector of rowSelectors) {
    const found = Array.from(document.querySelectorAll(selector)).filter((row) => {
      if (row.classList.contains('ag-header-row')) {
        return false;
      }
      return Boolean(row.querySelector('.ag-cell, td, [role="gridcell"]'));
    });
    if (found.length) {
      rows = found;
      break;
    }
  }

  const parseNumber = (text) => {
    if (!text || text === '-' || text === '—') {
      return null;
    }
    const cleaned = text.replace(/[^\\d.\\-]/g, '');
    if (!cleaned || cleaned === '-' || cleaned === '.') {
      return null;
    }
    const value = parseFloat(cleaned);
    return Number.isNaN(value) ? null : value;
  };

  const cellValue = (cells, key) => {
    const index = headerMap[key];
    if (index === undefined || index < 0) {
      return undefined;
    }
    return cells[index];
  };

  const results = [];
  for (const row of rows) {
    const cells = Array.from(
      row.querySelectorAll('.ag-cell, td, [role="gridcell"]')
    ).map((cell) => normalize(cell.textContent));

    if (!cells.length) {
      continue;
    }

    let symbol = cellValue(cells, 'symbol');
    let side = cellValue(cells, 'side');
    let entryPrice = parseNumber(cellValue(cells, 'entry_price'));
    let currentPrice = parseNumber(cellValue(cells, 'current_price'));
    let volume = parseNumber(cellValue(cells, 'volume'));
    let sl = parseNumber(cellValue(cells, 'sl'));
    let tp = parseNumber(cellValue(cells, 'tp'));
    let positionId = cellValue(cells, 'position_id');

    if (!headers.length) {
      symbol = cells.find((cell) => /^[A-Z]{3,12}$/.test(cell)) || symbol;
      side = cells.find((cell) => /^(Buy|Sell)$/i.test(cell)) || side;
      positionId = cells.find((cell) => /^\\d{4,}$/.test(cell)) || positionId;
      const numbers = cells.map(parseNumber).filter((value) => value !== null);
      entryPrice = entryPrice ?? (numbers[0] ?? null);
      currentPrice = currentPrice ?? (numbers[1] ?? null);
      volume = volume ?? (numbers[2] ?? null);
      sl = sl ?? (numbers[3] ?? null);
      tp = tp ?? (numbers[4] ?? null);
    }

    if (!symbol) {
      continue;
    }

    symbol = symbol.toUpperCase();

    if (!positionId) {
      positionId =
        row.getAttribute('row-id') ||
        row.getAttribute('data-position-id') ||
        row.getAttribute('data-id') ||
        '';
    }

    if (side) {
      const normalizedSide = side.toLowerCase();
      if (normalizedSide.includes('buy')) {
        side = 'Buy';
      } else if (normalizedSide.includes('sell')) {
        side = 'Sell';
      }
    }

    if (!positionId) {
      positionId = `${symbol}_${side || 'Unknown'}_${entryPrice}_${volume}`;
    }

    results.push({
      position_id: String(positionId),
      symbol,
      side: side || 'Unknown',
      entry_price: entryPrice,
      current_price: currentPrice,
      volume,
      sl,
      tp,
    });
  }

  return {
    headers,
    positions: results,
  };
}
"""


class CTraderCopyMonitor:
    """Monitor cTrader Copy investor positions with Playwright."""

    def __init__(
        self,
        copy_url: str,
        email: str,
        password: str,
        store: PositionStore,
        *,
        headless: bool = True,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        screenshots_dir: Path = SCREENSHOTS_DIR,
    ) -> None:
        self.copy_url = copy_url
        self.email = email
        self.password = password
        self.store = store
        self.headless = headless
        self.poll_interval = poll_interval
        self.screenshots_dir = screenshots_dir

    def run(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            page = context.new_page()

            try:
                self._open_copy_page(page)
                self._login_if_needed(page)
                self._open_positions_tab(page)
                self._wait_for_positions_table(page)
                logger.info(
                    "Monitoring positions every %ss. Output: %s",
                    self.poll_interval,
                    self.store.path,
                )
                self._monitor_loop(page)
            finally:
                context.close()
                browser.close()

    def _capture_selector_failure(
        self,
        page: Page,
        context: str,
        selectors: list[str],
        *,
        error: str | None = None,
    ) -> Path:
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_context = re.sub(r"[^a-zA-Z0-9_-]+", "_", context).strip("_")
        screenshot_path = self.screenshots_dir / f"failure_{safe_context}_{timestamp}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)

        logger.error(
            "Selector failure [%s]. Tried selectors: %s. Current URL: %s. "
            "Page title: %s. Screenshot saved to: %s%s",
            context,
            selectors,
            page.url,
            page.title(),
            screenshot_path,
            f". Error: {error}" if error else "",
        )
        return screenshot_path

    def _find_visible_locator(
        self,
        page: Page,
        selectors: list[str],
        context: str,
        *,
        required: bool = True,
        timeout_ms: int = 1_500,
    ):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=timeout_ms):
                    logger.debug("Found %s using selector: %s", context, selector)
                    return locator
            except PlaywrightTimeout:
                logger.debug("Selector not visible for %s: %s", context, selector)

        if required:
            self._capture_selector_failure(page, context, selectors)
            raise RuntimeError(f"Could not find {context}. See screenshot and logs.")
        return None

    def _open_copy_page(self, page: Page) -> None:
        logger.info("Opening COPY_URL: %s", self.copy_url)
        try:
            page.goto(self.copy_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)
        except PlaywrightTimeout as exc:
            self._capture_selector_failure(
                page,
                "open_copy_page",
                [self.copy_url],
                error=str(exc),
            )
            raise

        if self.copy_url not in page.url and "copyingAccount" in self.copy_url:
            logger.warning(
                "Redirected away from COPY_URL. Current URL: %s. Re-navigating.",
                page.url,
            )
            page.goto(self.copy_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)

    def _login_if_needed(self, page: Page) -> None:
        if self._is_logged_in(page):
            logger.info("Already logged in.")
            return

        if not self.email or not self.password:
            self._capture_selector_failure(
                page,
                "login_required",
                ["CTRADER_EMAIL", "CTRADER_PASSWORD"],
                error="Login required but credentials are not set in environment.",
            )
            raise RuntimeError(
                "Login required. Set CTRADER_EMAIL and CTRADER_PASSWORD in the environment."
            )

        logger.info("Login required. Signing in with cTrader ID...")
        self._perform_login(page)
        page.wait_for_timeout(3_000)

        if not self._is_logged_in(page):
            self._capture_selector_failure(
                page,
                "login_failed",
                ["logged-in markers"],
                error="Login completed but authenticated UI was not detected.",
            )
            raise RuntimeError("Login failed. Check credentials and COPY_URL.")

        logger.info("Login successful.")

        if self.copy_url not in page.url:
            logger.info("Returning to COPY_URL after login: %s", self.copy_url)
            page.goto(self.copy_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)

    def _is_logged_in(self, page: Page) -> bool:
        login_markers = [
            "text=Log in",
            "text=Login",
            "text=Sign in",
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
        ]

        for marker in login_markers:
            try:
                if page.locator(marker).first.is_visible(timeout=1_500):
                    return False
            except PlaywrightTimeout:
                continue

        logged_in_markers = POSITIONS_TABLE_SELECTORS + POSITIONS_HEADER_SELECTORS + [
            "text=Investment",
            "text=Copying",
        ]

        for marker in logged_in_markers:
            try:
                if page.locator(marker).first.is_visible(timeout=2_000):
                    return True
            except PlaywrightTimeout:
                continue

        return "login" not in page.url.lower() and "id.ctrader" not in page.url.lower()

    def _perform_login(self, page: Page) -> None:
        login_triggers = [
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "a:has-text('Log in')",
            "a:has-text('Login')",
            "a:has-text('Sign in')",
            "text=Log in",
            "text=Login",
            "text=Sign in",
        ]

        trigger = self._find_visible_locator(
            page,
            login_triggers,
            "login_trigger",
            required=False,
            timeout_ms=2_000,
        )
        if trigger is not None:
            trigger.click()
            page.wait_for_timeout(1_500)

        email_selectors = [
            "input[type='email']",
            "input[name='email']",
            "input[name='username']",
            "input[placeholder*='email' i]",
            "input[placeholder*='cTrader ID' i]",
            "#email",
            "#username",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name='password']",
            "#password",
        ]
        submit_selectors = [
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "input[type='submit']",
        ]

        email_input = self._find_visible_locator(page, email_selectors, "login_email")
        password_input = self._find_visible_locator(
            page, password_selectors, "login_password"
        )
        submit_button = self._find_visible_locator(page, submit_selectors, "login_submit")

        email_input.fill(self.email)
        password_input.fill(self.password)
        submit_button.click()

        try:
            page.wait_for_load_state("networkidle", timeout=60_000)
        except PlaywrightTimeout as exc:
            self._capture_selector_failure(
                page,
                "login_network_idle",
                submit_selectors,
                error=str(exc),
            )
            raise

    def _open_positions_tab(self, page: Page) -> None:
        for selector in POSITIONS_TAB_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=2_000):
                    locator.click()
                    page.wait_for_timeout(1_500)
                    logger.info("Opened Positions tab via selector: %s", selector)
                    return
            except PlaywrightTimeout:
                logger.debug("Positions tab selector not visible: %s", selector)

        positions_url = self._build_positions_url()
        if positions_url and positions_url not in page.url:
            logger.info("Positions tab not found. Navigating to: %s", positions_url)
            page.goto(positions_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)
            return

        logger.warning(
            "Positions tab selectors did not match. Continuing on current page: %s",
            page.url,
        )

    def _build_positions_url(self) -> str | None:
        base = self.copy_url.rstrip("/")
        if re.search(r"/copy/copyingAccount/[^/]+", base, re.IGNORECASE):
            if base.endswith("/positions"):
                return base
            return f"{base}/positions"
        return None

    def _wait_for_positions_table(self, page: Page) -> None:
        logger.info("Waiting for Positions table to become visible...")
        deadline = time.time() + 60

        while time.time() < deadline:
            for selector in POSITIONS_TABLE_SELECTORS:
                try:
                    if page.locator(selector).first.is_visible(timeout=1_000):
                        for header_selector in POSITIONS_HEADER_SELECTORS:
                            try:
                                if page.locator(header_selector).first.is_visible(
                                    timeout=1_000
                                ):
                                    logger.info(
                                        "Positions table visible. Table selector: %s. "
                                        "Header selector: %s",
                                        selector,
                                        header_selector,
                                    )
                                    return
                            except PlaywrightTimeout:
                                continue

                        logger.info(
                            "Positions table container visible via selector: %s",
                            selector,
                        )
                        return
                except PlaywrightTimeout:
                    continue

            page.wait_for_timeout(1_000)

        self._capture_selector_failure(
            page,
            "positions_table",
            POSITIONS_TABLE_SELECTORS + POSITIONS_HEADER_SELECTORS,
            error="Timed out after 60 seconds waiting for Positions table.",
        )
        raise RuntimeError("Positions table did not become visible within 60 seconds.")

    def _monitor_loop(self, page: Page) -> None:
        while True:
            try:
                self._poll_positions(page)
            except Exception as exc:
                logger.exception("Polling error: %s", exc)
                self._capture_selector_failure(
                    page,
                    "poll_positions",
                    POSITIONS_TABLE_SELECTORS,
                    error=str(exc),
                )
            time.sleep(self.poll_interval)

    def _poll_positions(self, page: Page) -> None:
        payload = page.evaluate(EXTRACT_POSITIONS_JS)
        if not isinstance(payload, dict) or "positions" not in payload:
            self._capture_selector_failure(
                page,
                "extract_positions",
                [EXTRACT_POSITIONS_JS[:80] + "..."],
                error="Unexpected extraction payload.",
            )
            raise RuntimeError("Failed to extract positions from page.")

        headers = payload.get("headers", [])
        if not headers:
            logger.warning(
                "No table headers detected during poll. URL: %s",
                page.url,
            )

        live_positions = self._normalize_positions(payload["positions"])
        live_ids = {position.position_id for position in live_positions}

        changed = False
        for position in live_positions:
            is_new = position.position_id not in self.store._positions
            if self.store.upsert(position):
                changed = True
                if is_new:
                    logger.info(
                    "New position: id=%s symbol=%s side=%s entry=%s current=%s "
                    "volume=%s sl=%s tp=%s",
                    position.position_id,
                    position.symbol,
                    position.side,
                    position.entry_price,
                    position.current_price,
                    position.volume,
                    position.sl,
                    position.tp,
                )

        for position_id in self.store.get_open_ids():
            if position_id not in live_ids:
                if self.store.mark_closed(position_id):
                    changed = True
                    logger.info("Position closed: id=%s", position_id)

        if changed:
            self.store.save()
            logger.info("Saved updates to %s", self.store.path)

    def _normalize_positions(self, raw_positions: list[dict]) -> list[Position]:
        normalized: list[Position] = []

        for raw in raw_positions:
            position_id = str(raw.get("position_id", "")).strip()
            symbol = str(raw.get("symbol", "")).upper().strip()
            side = str(raw.get("side", "Unknown"))
            entry_price = raw.get("entry_price")
            volume = raw.get("volume")

            if not position_id or not symbol:
                logger.debug("Skipping row without id/symbol: %s", raw)
                continue

            if entry_price is None or volume is None:
                logger.warning("Skipping incomplete position row: %s", raw)
                continue

            normalized.append(
                Position(
                    position_id=position_id,
                    symbol=symbol,
                    side=side,
                    entry_price=float(entry_price),
                    current_price=(
                        float(raw["current_price"])
                        if raw.get("current_price") is not None
                        else None
                    ),
                    volume=float(volume),
                    sl=float(raw["sl"]) if raw.get("sl") is not None else None,
                    tp=float(raw["tp"]) if raw.get("tp") is not None else None,
                )
            )

        return normalized


def main() -> None:
    logger.info("Using COPY_URL: %s", COPY_URL)
    store = PositionStore(POSITIONS_FILE)
    monitor = CTraderCopyMonitor(
        copy_url=COPY_URL,
        email=CTRADER_EMAIL,
        password=CTRADER_PASSWORD,
        store=store,
        headless=HEADLESS,
        screenshots_dir=SCREENSHOTS_DIR,
    )

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
