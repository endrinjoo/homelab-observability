"""A minimal Alertmanager webhook receiver that prints each alert. Standard library only."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9000
MAX_BODY_BYTES = 1_000_000


def format_alerts(payload: dict) -> list[str]:
    """Turn an Alertmanager webhook payload into one log line per alert."""
    lines = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        lines.append(
            "[{status}] {severity} {name}: {summary}".format(
                status=alert.get("status", "unknown").upper(),
                severity=labels.get("severity", "none"),
                name=labels.get("alertname", "unnamed"),
                summary=annotations.get("summary", "no summary"),
            )
        )
    return lines


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError("bad body size")
            payload = json.loads(self.rfile.read(length))
        except ValueError as error:
            self.send_error(400, str(error))
            return
        if not isinstance(payload, dict):
            self.send_error(400, "payload must be a JSON object")
            return
        for line in format_alerts(payload):
            print(line)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        """Keep the log to alerts only."""


if __name__ == "__main__":
    print(f"alert-logger listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
