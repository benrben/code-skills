#!/usr/bin/env python3
"""Start an application once and load it.

The check answers one question the test suite cannot: does the application run?
It starts the configured command with a free port, waits for an HTTP answer,
and for a web UI loads the page in a headless browser and fails on any page or
console error. The process is always stopped afterwards.

Usage:
  smoke_check.py --start "npm start" [--port-env PORT] [--path /] [--browser]
  smoke_check.py --url http://127.0.0.1:3000/ [--browser]   (server already running)

Exit 0 when the application answered (and, with --browser, loaded with zero
errors); exit 1 otherwise. Every outcome is printed as SMOKE=PASS or SMOKE=FAIL.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Sequence

VERSION = "1.0.0"
DEFAULT_TIMEOUT_SECONDS = 60
POLL_SECONDS = 0.25
STOP_GRACE_SECONDS = 5
OUTPUT_TAIL = 4000
DEFAULT_FAIL_PATTERN = r"(?i)\b(error|exception|failed|could not)\b"
BROWSER_MISSING = (
    "no headless browser available: install one with "
    "`python3 -m pip install playwright && python3 -m playwright install chromium` "
    "or `npm install -D playwright && npx playwright install chromium`"
)
NODE_LOADER = """
const [modulePath, url, timeout, selector] = process.argv.slice(1);
const { chromium } = require(modulePath);
(async () => {
  const errors = [];
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  await page.goto(url, { waitUntil: "networkidle", timeout: Number(timeout) });
  await page.waitForTimeout(500);
  const found = !selector || (await page.$(selector)) !== null;
  const body = await page.innerText("body");
  await browser.close();
  process.stdout.write(JSON.stringify({ errors, found, body }));
})().catch((error) => { console.error(String(error)); process.exit(1); });
"""


@dataclasses.dataclass(frozen=True)
class PageExpectations:
    """What a loaded page must show: a selector, a text, and no failure-looking text."""

    selector: str | None = None
    text: str | None = None
    fail_on_text: str | None = DEFAULT_FAIL_PATTERN


@dataclasses.dataclass(frozen=True)
class SmokeOptions:
    start: str | None
    cwd: Path
    url: str | None
    path: str
    port: int | None
    port_env: str
    browser: bool
    timeout: float
    env: dict[str, str]
    expectations: PageExpectations = PageExpectations()


@dataclasses.dataclass
class SmokeOutcome:
    passed: bool
    url: str
    status: int | None = None
    errors: list[str] = dataclasses.field(default_factory=list)
    reason: str = ""

    def lines(self) -> list[str]:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [f"SMOKE={verdict} url={self.url} http={self.status}"]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        lines.extend(f"  {error}" for error in self.errors)
        return lines


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_status(url: str, timeout: float) -> int | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "smoke-check"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_http(
    url: str,
    deadline_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """The first HTTP status the URL answers with, or None once the deadline passes."""
    deadline = clock() + deadline_seconds
    while True:
        status = http_status(url, 5)
        if status is not None:
            return status
        if clock() >= deadline:
            return None
        sleep(POLL_SECONDS)


def status_ok(status: int) -> bool:
    return 200 <= status < 400


def start_process(
    command: str, cwd: Path, env: dict[str, str]
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )


def signal_group(process: Any, signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (AttributeError, ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signum)


def stop_process(process: Any, grace_seconds: float = STOP_GRACE_SECONDS) -> str:
    """Stop the application and its children; returns the tail of its output."""
    signal_group(process, signal.SIGTERM)
    try:
        output, _ = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        signal_group(process, signal.SIGKILL)
        output, _ = process.communicate()
    return str(output or "")[-OUTPUT_TAIL:]


def record_console(errors: list[str], message: Any) -> None:
    if message.type == "error":
        errors.append(f"console: {message.text}")


def snippet(text: str, index: int) -> str:
    start = max(0, index - 40)
    return " ".join(text[start : index + 80].split())


def failure_text_error(pattern: str | None, body_text: str) -> str | None:
    match = re.search(pattern, body_text) if pattern else None
    if match is None:
        return None
    return f"page text looks like a failure: {snippet(body_text, match.start())!r}"


def page_expectation_errors(
    expectations: PageExpectations, selector_found: bool, body_text: str
) -> list[str]:
    errors: list[str] = []
    if expectations.selector and not selector_found:
        errors.append(f"expected selector not found: {expectations.selector}")
    if expectations.text and expectations.text not in body_text:
        errors.append(f"expected text not found: {expectations.text!r}")
    failure = failure_text_error(expectations.fail_on_text, body_text)
    if failure is not None:
        errors.append(failure)
    return errors


def collect_page_errors(
    sync_playwright: Any, url: str, timeout: float, expectations: PageExpectations
) -> list[str]:
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
        page.on("console", lambda message: record_console(errors, message))
        page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        page.wait_for_timeout(500)
        found = (
            expectations.selector is None
            or page.query_selector(expectations.selector) is not None
        )
        errors.extend(
            page_expectation_errors(expectations, found, page.inner_text("body"))
        )
        browser.close()
    return errors


def python_playwright_errors(
    url: str, timeout: float, expectations: PageExpectations
) -> list[str] | None:
    try:
        module = importlib.import_module("playwright.sync_api")
    except ImportError:
        return None
    return collect_page_errors(module.sync_playwright, url, timeout, expectations)


def find_node_playwright(cwd: Path) -> Path | None:
    for name in ("playwright", "playwright-core"):
        candidate = cwd / "node_modules" / name
        if candidate.is_dir():
            return candidate
    return None


def node_playwright_errors(
    url: str, cwd: Path, timeout: float, expectations: PageExpectations
) -> list[str] | None:
    module_dir = find_node_playwright(cwd)
    if module_dir is None:
        return None
    arguments = [
        str(module_dir),
        url,
        str(int(timeout * 1000)),
        expectations.selector or "",
    ]
    completed = subprocess.run(
        ["node", "-e", NODE_LOADER, *arguments],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout + 30,
        check=False,
    )
    if completed.returncode != 0:
        return [f"browser runner failed: {completed.stderr.strip()[-2000:]}"]
    loaded = json.loads(completed.stdout or "{}")
    errors = [str(item) for item in loaded.get("errors", [])]
    errors.extend(
        page_expectation_errors(
            expectations, bool(loaded.get("found", True)), str(loaded.get("body", ""))
        )
    )
    return errors


def browser_errors(
    url: str, cwd: Path, timeout: float, expectations: PageExpectations
) -> list[str]:
    errors = python_playwright_errors(url, timeout, expectations)
    if errors is None:
        errors = node_playwright_errors(url, cwd, timeout, expectations)
    if errors is None:
        return [BROWSER_MISSING]
    return errors


def judge(url: str, status: int | None, options: SmokeOptions) -> SmokeOutcome:
    if status is None:
        return SmokeOutcome(
            False, url, reason=f"no HTTP answer within {options.timeout:g}s"
        )
    if not status_ok(status):
        return SmokeOutcome(False, url, status, reason=f"HTTP {status}")
    if not options.browser:
        return SmokeOutcome(True, url, status)
    errors = browser_errors(url, options.cwd, options.timeout, options.expectations)
    reason = "page loaded with errors" if errors else ""
    return SmokeOutcome(not errors, url, status, errors, reason)


def build_env(options: SmokeOptions, port: int | None) -> dict[str, str]:
    env = {**os.environ, **options.env}
    if port is not None:
        env[options.port_env] = str(port)
    return env


def target_url(options: SmokeOptions, port: int | None) -> str:
    if options.url:
        return options.url
    return f"http://127.0.0.1:{port}{options.path}"


def resolve_port(options: SmokeOptions) -> int | None:
    if options.start is None:
        return options.port
    return options.port if options.port is not None else free_port()


def launch(
    options: SmokeOptions,
    port: int | None,
    starter: Callable[[str, Path, dict[str, str]], Any],
) -> Any:
    if options.start is None:
        return None
    return starter(options.start, options.cwd, build_env(options, port))


def with_process_output(outcome: SmokeOutcome, output: str) -> SmokeOutcome:
    if not outcome.passed and output:
        outcome.errors.append(f"process output (tail): {output}")
    return outcome


def check(
    options: SmokeOptions,
    starter: Callable[[str, Path, dict[str, str]], Any] = start_process,
) -> SmokeOutcome:
    port = resolve_port(options)
    url = target_url(options, port)
    process = launch(options, port, starter)
    output = ""
    try:
        outcome = judge(url, wait_for_http(url, options.timeout), options)
    finally:
        if process is not None:
            output = stop_process(process)
    return with_process_output(outcome, output)


def parse_env(pairs: Sequence[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"--env expects KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start", help="shell command that starts the application")
    parser.add_argument("--cwd", default=".", help="directory to start it in")
    parser.add_argument(
        "--url", help="URL to load (default: http://127.0.0.1:PORT/PATH)"
    )
    parser.add_argument("--path", default="/", help="path to load on the chosen port")
    parser.add_argument("--port", type=int, help="port to use (default: a free port)")
    parser.add_argument(
        "--port-env", default="PORT", help="environment variable that carries the port"
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="load the page in a headless browser and fail on any page or console error",
    )
    parser.add_argument(
        "--expect-selector",
        help="with --browser: a CSS selector that only a working page shows (e.g. canvas)",
    )
    parser.add_argument(
        "--expect-text", help="with --browser: text the loaded page must contain"
    )
    parser.add_argument(
        "--fail-on-text",
        default=DEFAULT_FAIL_PATTERN,
        metavar="REGEX",
        help="with --browser: fail when the page text matches (default: error-looking words; pass '' to disable)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="seconds to wait for the first HTTP answer",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra environment",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def options_from_args(args: argparse.Namespace) -> SmokeOptions:
    if not args.start and not args.url:
        raise ValueError("pass --start (a command) or --url (a running server)")
    return SmokeOptions(
        start=args.start,
        cwd=Path(args.cwd).resolve(),
        url=args.url,
        path=args.path,
        port=args.port,
        port_env=args.port_env,
        browser=args.browser,
        timeout=args.timeout,
        env=parse_env(args.env),
        expectations=PageExpectations(
            args.expect_selector, args.expect_text, args.fail_on_text or None
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = options_from_args(parse_args(argv))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    outcome = check(options)
    print("\n".join(outcome.lines()))
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
