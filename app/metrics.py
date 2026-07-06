from collections import defaultdict
from threading import Lock


def _escape_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())) + "}"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._http_requests: defaultdict[tuple[str, str, str], int] = defaultdict(int)
        self._http_duration_sum: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._http_duration_count: defaultdict[tuple[str, str], int] = defaultdict(int)
        self._media_downloads: defaultdict[tuple[str, str], int] = defaultdict(int)
        self._rate_limit_exceeded: defaultdict[tuple[str, str], int] = defaultdict(int)
        self._websocket_connections = 0

    def record_http_request(self, *, method: str, endpoint: str, status_code: int, duration_seconds: float) -> None:
        with self._lock:
            self._http_requests[(method, endpoint, str(status_code))] += 1
            self._http_duration_sum[(method, endpoint)] += duration_seconds
            self._http_duration_count[(method, endpoint)] += 1

    def record_media_download(self, *, media_type: str, status: str) -> None:
        with self._lock:
            self._media_downloads[(media_type, status)] += 1

    def record_rate_limit_exceeded(self, *, action: str, actor: str) -> None:
        with self._lock:
            self._rate_limit_exceeded[(action, actor)] += 1

    def websocket_connected(self) -> None:
        with self._lock:
            self._websocket_connections += 1

    def websocket_disconnected(self) -> None:
        with self._lock:
            self._websocket_connections = max(0, self._websocket_connections - 1)

    def render_prometheus(self) -> str:
        with self._lock:
            http_requests = dict(self._http_requests)
            duration_sum = dict(self._http_duration_sum)
            duration_count = dict(self._http_duration_count)
            media_downloads = dict(self._media_downloads)
            rate_limit_exceeded = dict(self._rate_limit_exceeded)
            websocket_connections = self._websocket_connections

        lines = [
            "# HELP chat_audit_http_requests_total Total HTTP requests.",
            "# TYPE chat_audit_http_requests_total counter",
        ]
        for (method, endpoint, status_code), value in sorted(http_requests.items()):
            lines.append(f"chat_audit_http_requests_total{_labels(method=method, endpoint=endpoint, status=status_code)} {value}")

        lines.extend(
            [
                "# HELP chat_audit_http_request_duration_seconds HTTP request duration.",
                "# TYPE chat_audit_http_request_duration_seconds summary",
            ]
        )
        for (method, endpoint), value in sorted(duration_count.items()):
            lines.append(f"chat_audit_http_request_duration_seconds_count{_labels(method=method, endpoint=endpoint)} {value}")
            lines.append(f"chat_audit_http_request_duration_seconds_sum{_labels(method=method, endpoint=endpoint)} {duration_sum[(method, endpoint)]:.6f}")

        lines.extend(
            [
                "# HELP chat_audit_websocket_connections Active WebSocket connections.",
                "# TYPE chat_audit_websocket_connections gauge",
                f"chat_audit_websocket_connections {websocket_connections}",
                "# HELP chat_audit_media_download_total Media download attempts.",
                "# TYPE chat_audit_media_download_total counter",
            ]
        )
        for (media_type, status), value in sorted(media_downloads.items()):
            lines.append(f"chat_audit_media_download_total{_labels(media_type=media_type, status=status)} {value}")

        lines.extend(
            [
                "# HELP chat_audit_rate_limit_exceeded_total Rate limit exceeded events.",
                "# TYPE chat_audit_rate_limit_exceeded_total counter",
            ]
        )
        for (action, actor), value in sorted(rate_limit_exceeded.items()):
            lines.append(f"chat_audit_rate_limit_exceeded_total{_labels(action=action, actor=actor)} {value}")
        return "\n".join(lines) + "\n"


metrics_registry = MetricsRegistry()
