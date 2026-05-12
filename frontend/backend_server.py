from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
import urllib.request


def is_api_available(base_url: str, *, timeout: float = 0.5) -> bool:
	try:
		with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/v1/fe/bootstrap", timeout=timeout):
			return True
	except Exception:
		return False


def ensure_api_server(base_url: str, *, host: str = "127.0.0.1", port: int = 8000) -> None:
	if is_api_available(base_url):
		return

	parsed = urlparse(base_url)
	if parsed.hostname:
		host = parsed.hostname
	if parsed.port:
		port = parsed.port

	def run_server() -> None:
		import uvicorn

		uvicorn.run("api.api:app", host=host, port=port, log_level="warning")

	thread = threading.Thread(target=run_server, name="veritas-api", daemon=True)
	thread.start()

	deadline = time.monotonic() + 8.0
	while time.monotonic() < deadline:
		if is_api_available(base_url):
			return
		time.sleep(0.15)
