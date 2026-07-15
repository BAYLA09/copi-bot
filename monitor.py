from __future__ import annotations

import logging
import os
import re
import sys
import time
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
TARGET_SYMBOL = "XAUUSD"

COPY_URL = os.getenv("COPY_URL", "").strip()
CTRADER_EMAIL = os.getenv("CTRADER_EMAIL", "").strip()
CTRADER_PASSWORD = os.getenv("CTRADER_PASSWORD", "").strip()
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes"}
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))

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
  headers.forEach((header, index) => {
    const key = header.toLowerCase();
    headerMap[key] = index;
    if (key.includes('symbol')) headerMap.symbol = index;
    if (key.includes('direction') || key === 'side' || key === 'type') {
      headerMap.side = index;
    }
    if (
      key.includes('entry') ||
      (key.includes('open') && key.includes('price')) ||
      key === 'open price'
    ) {
      headerMap.entry_price = index;
    }
    if (key === 'price' && headerMap.entry_price === undefined) {
      headerMap.entry_price = index;
    }
    if (key.includes('volume') || key.includes('qty') || key.includes('size')) {
      headerMap.volume = index;
    }
    if (key.includes('id') || key === '#') {
      headerMap.position_id = index;
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
    const cleaned = (text || '').replace(/[^\\d.\\-]/g, '');
    const value = parseFloat(cleaned);
    return Number.isNaN(value) ? null : value;
  };

  const results = [];
  for (const row of rows) {
    const cells = Array.from(
      row.querySelectorAll('.ag-cell, td, [role="gridcell"]')
    ).map((cell) => normalize(cell.textContent));

    if (!cells.length) {
      continue;
    }

    let symbol;
    let side;
    let entryPrice;
    let volume;
    let positionId;

    if (headers.length) {
      symbol = cells[headerMap.symbol ?? -1];
      side = cells[headerMap.side ?? -1];
      entryPrice = parseNumber(cells[headerMap.entry_price ?? -1]);
      volume = parseNumber(cells[headerMap.volume ?? -1]);
      positionId = cells[headerMap.position_id ?? -1];
    } else {
      const rowText = cells.join(' ');
      if (!/XAUUSD/i.test(rowText)) {
        continue;
      }
      symbol = cells.find((cell) => /XAUUSD/i.test(cell)) || 'XAUUSD';
      side = cells.find((cell) => /^(Buy|Sell)$/i.test(cell)) || '';
      const numbers = cells.map(parseNumber).filter((value) => value !== null);
      entryPrice = numbers.length ? numbers[0] : null;
      volume = numbers.length > 1 ? numbers[1] : null;
      positionId = cells.find((cell) => /^\\d{5,}$/.test(cell)) || '';
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
      volume,
    });
  }

  return results;
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
        target_symbol: str = TARGET_SYMBOL,
    ) -> None:
        self.copy_url = copy_url
        self.email = email
        self.password = password
        self.store = store
        self.headless = headless
        self.poll_interval = poll_interval
        self.target_symbol = target_symbol.upper()

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
                logger.info(
                    "Monitoring %s positions every %ss. Output: %s",
                    self.target_symbol,
                    self.poll_interval,
                    self.store.path,
                )
                self._monitor_loop(page)
            finally:
                context.close()
                browser.close()

    def _open_copy_page(self, page: Page) -> None:
        logger.info("Opening cTrader Copy page: %s", self.copy_url)
        page.goto(self.copy_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_000)

    def _login_if_needed(self, page: Page) -> None:
        if self._is_logged_in(page):
            logger.info("Already logged in.")
            return

        if not self.email or not self.password:
            raise RuntimeError(
                "Login required. Set CTRADER_EMAIL and CTRADER_PASSWORD in the environment."
            )

        logger.info("Login required. Signing in with cTrader ID...")
        self._perform_login(page)
        page.wait_for_timeout(3_000)

        if not self._is_logged_in(page):
            raise RuntimeError("Login failed. Check credentials and COPY_URL.")

        logger.info("Login successful.")

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

        logged_in_markers = [
            "text=Positions",
            "text=Strategies",
            "text=Investment",
            "text=Copying",
            ".ag-root",
            "[role='grid']",
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

        for trigger in login_triggers:
            locator = page.locator(trigger).first
            try:
                if locator.is_visible(timeout=1_500):
                    locator.click()
                    page.wait_for_timeout(1_500)
                    break
            except PlaywrightTimeout:
                continue

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

        email_input = self._first_visible_locator(page, email_selectors)
        password_input = self._first_visible_locator(page, password_selectors)

        if email_input is None or password_input is None:
            raise RuntimeError("Could not find cTrader login form fields.")

        email_input.fill(self.email)
        password_input.fill(self.password)

        submit_selectors = [
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
            "input[type='submit']",
        ]
        submit_button = self._first_visible_locator(page, submit_selectors)
        if submit_button is None:
            raise RuntimeError("Could not find cTrader login submit button.")

        submit_button.click()
        page.wait_for_load_state("networkidle", timeout=60_000)

        if self.copy_url not in page.url:
            page.goto(self.copy_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)

    def _first_visible_locator(self, page: Page, selectors: list[str]):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=1_500):
                    return locator
            except PlaywrightTimeout:
                continue
        return None

    def _open_positions_tab(self, page: Page) -> None:
        tab_selectors = [
            "role=tab[name=/Positions/i]",
            "button:has-text('Positions')",
            "a:has-text('Positions')",
            "[data-testid*='positions' i]",
            "text=Positions",
        ]

        for selector in tab_selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=2_000):
                    locator.click()
                    page.wait_for_timeout(1_500)
                    logger.info("Opened Positions tab.")
                    return
            except PlaywrightTimeout:
                continue

        if "/positions" not in page.url.lower():
            positions_url = self._build_positions_url()
            if positions_url:
                logger.info("Navigating directly to positions URL: %s", positions_url)
                page.goto(positions_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2_000)
                return

        logger.warning("Positions tab not found explicitly. Continuing with current view.")

    def _build_positions_url(self) -> str | None:
        if not self.copy_url:
            return None

        base = self.copy_url.rstrip("/")
        if re.search(r"/copy/copyingAccount/\d+", base, re.IGNORECASE):
            return f"{base}/positions"

        match = re.search(r"(https?://[^/]+)", base)
        if match:
            return f"{match.group(1)}/copy/positions"

        return None

    def _monitor_loop(self, page: Page) -> None:
        while True:
            try:
                self._poll_positions(page)
            except Exception:
                logger.exception("Polling error. Retrying on next interval.")
            time.sleep(self.poll_interval)

    def _poll_positions(self, page: Page) -> None:
        raw_positions = page.evaluate(EXTRACT_POSITIONS_JS)
        live_positions = self._normalize_positions(raw_positions)
        live_ids = {position.position_id for position in live_positions}

        changed = False
        for position in live_positions:
            is_new = self.store.upsert(position)
            if is_new:
                changed = True
                logger.info(
                    "New position detected: id=%s side=%s entry=%s volume=%s",
                    position.position_id,
                    position.side,
                    position.entry_price,
                    position.volume,
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
            symbol = str(raw.get("symbol", "")).upper()
            if symbol != self.target_symbol:
                continue

            entry_price = raw.get("entry_price")
            volume = raw.get("volume")
            if entry_price is None or volume is None:
                logger.debug("Skipping incomplete row: %s", raw)
                continue

            side = str(raw.get("side", "Unknown"))
            position_id = str(raw.get("position_id", "")).strip()
            if not position_id:
                continue

            normalized.append(
                Position(
                    position_id=position_id,
                    symbol=symbol,
                    side=side,
                    entry_price=float(entry_price),
                    volume=float(volume),
                )
            )

        return normalized


def validate_config() -> None:
    if not COPY_URL:
        raise SystemExit(
            "COPY_URL is required. Example: https://ct.yourbroker.com/copy/copyingAccount/123456"
        )


def main() -> None:
    validate_config()
    store = PositionStore(POSITIONS_FILE)
    monitor = CTraderCopyMonitor(
        copy_url=COPY_URL,
        email=CTRADER_EMAIL,
        password=CTRADER_PASSWORD,
        store=store,
        headless=HEADLESS,
    )

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
