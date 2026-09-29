#!/usr/bin/env python3
"""Local visual retry tool for the redeem view endpoint."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


TARGET_URL = "https://timoes.me/api/redeem/view"
DEFAULT_CODE = "17ebff10-fb12-49c2-91da-d91b602a4170"
MIN_INTERVAL_SECONDS = 1.0
MAX_LOGS = 200

_state_lock = threading.Lock()
_stop_event = threading.Event()
_worker: threading.Thread | None = None
_state: dict[str, Any] = {
    "running": False,
    "success": False,
    "attempts": 0,
    "lastStatus": None,
    "startedAt": None,
    "finishedAt": None,
    "nextAttemptAt": None,
    "message": "等待启动",
    "logs": [],
}


PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Redeem 请求重试器</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: light-dark(#f5f6f8, #101214);
      --surface: light-dark(#ffffff, #191c20);
      --surface-2: light-dark(#f0f2f5, #22262b);
      --text: light-dark(#1a1d21, #f1f3f5);
      --muted: light-dark(#626a73, #a6adb6);
      --border: light-dark(#d9dde3, #363b42);
      --accent: light-dark(#1769e0, #69a5ff);
      --accent-text: #ffffff;
      --ok: light-dark(#157347, #5bd39a);
      --danger: light-dark(#b42318, #ff7b72);
      --warn: light-dark(#8a5900, #f2bf63);
      --shadow: light-dark(rgba(23, 30, 40, .08), rgba(0, 0, 0, .28));
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }
    button, input { font: inherit; }
    button:focus-visible, input:focus-visible {
      outline: 3px solid color-mix(in srgb, var(--accent) 35%, transparent);
      outline-offset: 2px;
    }
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }
    .header-inner, main {
      width: min(100% - 32px, 1040px);
      margin: 0 auto;
    }
    .header-inner {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 17px; font-weight: 500; letter-spacing: 0; }
    .endpoint {
      max-width: 65%;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      text-align: right;
    }
    main { padding: 20px 0 32px; }
    .control-panel, .timeline-panel, .response-panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 8px 26px var(--shadow);
    }
    .control-panel { padding: 18px; }
    .fields {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) 150px auto;
      align-items: end;
      gap: 12px;
    }
    label { display: block; color: var(--muted); font-size: 12px; }
    label span { display: block; margin-bottom: 6px; }
    input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface-2);
      color: var(--text);
      padding: 0 10px;
    }
    #code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .actions { display: flex; gap: 8px; }
    button {
      height: 38px;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0 15px;
      background: var(--surface-2);
      color: var(--text);
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
    button.danger { color: var(--danger); }
    button:disabled { opacity: .45; cursor: default; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      margin-top: 18px;
      border-top: 1px solid var(--border);
    }
    .metric { padding: 14px 14px 0 0; min-width: 0; }
    .metric-name { color: var(--muted); font-size: 12px; }
    .metric-value { display: block; margin-top: 3px; font-size: 18px; font-weight: 500; overflow-wrap: anywhere; }
    .metric-value.small { font-size: 14px; padding-top: 3px; }
    .status-running { color: var(--accent); }
    .status-ok { color: var(--ok); }
    .status-error { color: var(--danger); }
    .timeline-panel, .response-panel { margin-top: 14px; padding: 16px 18px; }
    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    h2 { margin: 0; font-size: 14px; font-weight: 500; letter-spacing: 0; }
    .secondary { color: var(--muted); font-size: 12px; }
    .attempt-track {
      min-height: 34px;
      display: flex;
      align-items: center;
      gap: 5px;
      overflow: hidden;
    }
    .attempt-dot {
      flex: 0 0 10px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--danger);
      border: 2px solid color-mix(in srgb, var(--danger) 25%, var(--surface));
    }
    .attempt-dot.ok { background: var(--ok); border-color: color-mix(in srgb, var(--ok) 25%, var(--surface)); }
    .attempt-dot.network { background: var(--warn); border-color: color-mix(in srgb, var(--warn) 25%, var(--surface)); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { padding: 8px 10px; border-top: 1px solid var(--border); text-align: left; white-space: nowrap; }
    th { color: var(--muted); font-weight: 400; }
    td:last-child { white-space: normal; min-width: 260px; overflow-wrap: anywhere; }
    .status-cell { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 500; }
    pre {
      min-height: 96px;
      max-height: 280px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      border-radius: 6px;
      background: var(--surface-2);
      color: var(--text);
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    @media (max-width: 760px) {
      .header-inner, main { width: min(100% - 20px, 1040px); }
      .header-inner { align-items: flex-start; flex-direction: column; justify-content: center; padding: 10px 0; gap: 3px; }
      .endpoint { max-width: 100%; text-align: left; }
      .fields { grid-template-columns: 1fr 120px; }
      .actions { grid-column: 1 / -1; }
      .actions button { flex: 1; }
      .metrics { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 430px) {
      .fields { grid-template-columns: 1fr; }
      .actions { grid-column: auto; }
      .metrics { grid-template-columns: 1fr; }
      .metric { padding-top: 10px; }
      .control-panel, .timeline-panel, .response-panel { padding-left: 12px; padding-right: 12px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>Redeem 请求重试器</h1>
      <div class="endpoint">POST https://timoes.me/api/redeem/view</div>
    </div>
  </header>
  <main>
    <section class="control-panel" aria-label="请求控制">
      <div class="fields">
        <label><span>兑换码</span><input id="code" autocomplete="off" spellcheck="false" value="17ebff10-fb12-49c2-91da-d91b602a4170"></label>
        <label><span>间隔（秒）</span><input id="interval" type="number" min="1" max="300" step="1" value="1"></label>
        <div class="actions">
          <button class="primary" id="start" type="button">启动重试</button>
          <button class="danger" id="stop" type="button" disabled>停止</button>
        </div>
      </div>
      <div class="metrics" aria-live="polite">
        <div class="metric"><span class="metric-name">运行状态</span><strong class="metric-value small" id="run-state">等待启动</strong></div>
        <div class="metric"><span class="metric-name">请求次数</span><strong class="metric-value" id="attempts">0</strong></div>
        <div class="metric"><span class="metric-name">最近状态</span><strong class="metric-value" id="last-status">--</strong></div>
        <div class="metric"><span class="metric-name">已运行</span><strong class="metric-value" id="elapsed">00:00</strong></div>
      </div>
    </section>

    <section class="timeline-panel">
      <div class="section-head"><h2>请求轨迹</h2><span class="secondary" id="next-attempt">尚未排队</span></div>
      <div class="attempt-track" id="track" aria-label="最近请求状态"></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>时间</th><th>HTTP</th><th>耗时</th><th>响应摘要</th></tr></thead>
          <tbody id="log-body"><tr><td colspan="5" class="secondary">暂无请求</td></tr></tbody>
        </table>
      </div>
    </section>

    <section class="response-panel">
      <div class="section-head"><h2>最近响应</h2><span class="secondary">收到 200 后自动停止</span></div>
      <pre id="response">尚无响应</pre>
    </section>
  </main>
  <script>
    const byId = (id) => document.getElementById(id);
    const els = {
      code: byId('code'), interval: byId('interval'), start: byId('start'), stop: byId('stop'),
      runState: byId('run-state'), attempts: byId('attempts'), lastStatus: byId('last-status'),
      elapsed: byId('elapsed'), nextAttempt: byId('next-attempt'), track: byId('track'),
      logBody: byId('log-body'), response: byId('response')
    };
    let currentState = null;

    function formatElapsed(startedAt, finishedAt) {
      if (!startedAt) return '00:00';
      const end = finishedAt || Date.now() / 1000;
      const seconds = Math.max(0, Math.floor(end - startedAt));
      const minutes = Math.floor(seconds / 60);
      return String(minutes).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
    }

    function statusClass(state) {
      if (state.success) return 'status-ok';
      if (state.running) return 'status-running';
      if (state.lastStatus && state.lastStatus !== 200) return 'status-error';
      return '';
    }

    function render(state) {
      currentState = state;
      els.runState.textContent = state.message;
      els.runState.className = 'metric-value small ' + statusClass(state);
      els.attempts.textContent = state.attempts;
      els.lastStatus.textContent = state.lastStatus === null ? '--' : (state.lastStatus || '网络错误');
      els.lastStatus.className = 'metric-value ' + (state.lastStatus === 200 ? 'status-ok' : state.lastStatus ? 'status-error' : '');
      els.elapsed.textContent = formatElapsed(state.startedAt, state.finishedAt);
      els.start.disabled = state.running;
      els.stop.disabled = !state.running;
      els.code.disabled = state.running;
      els.interval.disabled = state.running;

      if (state.running && state.nextAttemptAt) {
        const left = Math.max(0, Math.ceil(state.nextAttemptAt - Date.now() / 1000));
        els.nextAttempt.textContent = left ? `${left} 秒后重试` : '正在请求';
      } else {
        els.nextAttempt.textContent = state.success ? '已成功' : '尚未排队';
      }

      const recent = state.logs.slice(-50);
      els.track.replaceChildren(...recent.map((item) => {
        const dot = document.createElement('span');
        dot.className = 'attempt-dot ' + (item.status === 200 ? 'ok' : item.status === 0 ? 'network' : '');
        dot.title = `第 ${item.attempt} 次: ${item.status || '网络错误'}`;
        return dot;
      }));

      const rows = state.logs.slice(-12).reverse();
      if (!rows.length) {
        els.logBody.innerHTML = '<tr><td colspan="5" class="secondary">暂无请求</td></tr>';
        els.response.textContent = '尚无响应';
        return;
      }
      els.logBody.replaceChildren(...rows.map((item) => {
        const tr = document.createElement('tr');
        const values = [item.attempt, item.time, item.status || 'ERR', `${item.durationMs} ms`, item.preview || '(空响应)'];
        values.forEach((value, index) => {
          const td = document.createElement('td');
          td.textContent = value;
          if (index === 2) td.className = 'status-cell ' + (item.status === 200 ? 'status-ok' : 'status-error');
          tr.appendChild(td);
        });
        return tr;
      }));
      els.response.textContent = rows[0].body || '(空响应)';
    }

    async function api(path, body) {
      const response = await fetch(path, {
        method: body ? 'POST' : 'GET',
        headers: body ? {'Content-Type': 'application/json'} : {},
        body: body ? JSON.stringify(body) : undefined,
        cache: 'no-store'
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }

    async function refresh() {
      try { render(await api('/api/state')); }
      catch (error) {
        els.runState.textContent = '本地服务连接失败';
        els.runState.className = 'metric-value small status-error';
      }
    }

    els.start.addEventListener('click', async () => {
      const code = els.code.value.trim();
      const interval = Number(els.interval.value);
      if (!code) { els.code.focus(); return; }
      try {
        localStorage.setItem('redeemRetryIntervalV2', String(interval));
        render(await api('/api/start', {code, interval}));
      } catch (error) { window.alert(error.message); }
    });
    els.stop.addEventListener('click', async () => {
      try { render(await api('/api/stop', {})); }
      catch (error) { window.alert(error.message); }
    });
    const savedInterval = Number(localStorage.getItem('redeemRetryIntervalV2'));
    if (savedInterval >= 1 && savedInterval <= 300) els.interval.value = savedInterval;
    refresh();
    window.setInterval(refresh, 750);
  </script>
</body>
</html>'''


def _public_state() -> dict[str, Any]:
    with _state_lock:
        return {
            "running": _state["running"],
            "success": _state["success"],
            "attempts": _state["attempts"],
            "lastStatus": _state["lastStatus"],
            "startedAt": _state["startedAt"],
            "finishedAt": _state["finishedAt"],
            "nextAttemptAt": _state["nextAttemptAt"],
            "message": _state["message"],
            "logs": list(_state["logs"]),
        }


def _make_request(code: str) -> tuple[int, str]:
    body = json.dumps({"code": code}).encode("utf-8")
    request = urllib.request.Request(
        TARGET_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "redeem-retry-tool/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, str(error)


def _retry_loop(code: str, interval: float) -> None:
    while not _stop_event.is_set():
        started = time.monotonic()
        http_status, body = _make_request(code)
        request_seconds = time.monotonic() - started
        duration_ms = round(request_seconds * 1000)
        delay = max(0.0, interval - request_seconds)
        now = time.time()
        preview = " ".join(body.split())[:160]

        with _state_lock:
            _state["attempts"] += 1
            _state["lastStatus"] = http_status
            _state["logs"].append(
                {
                    "attempt": _state["attempts"],
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "status": http_status,
                    "durationMs": duration_ms,
                    "preview": preview,
                    "body": body[:8000],
                }
            )
            _state["logs"] = _state["logs"][-MAX_LOGS:]
            if http_status == HTTPStatus.OK:
                _state.update(
                    running=False,
                    success=True,
                    finishedAt=now,
                    nextAttemptAt=None,
                    message="请求成功",
                )
                return
            _state["message"] = "等待重试"
            _state["nextAttemptAt"] = now + delay

        if _stop_event.wait(delay):
            break

    with _state_lock:
        _state.update(
            running=False,
            finishedAt=time.time(),
            nextAttemptAt=None,
            message="已停止",
        )


def _start(code: str, interval: float) -> tuple[bool, str]:
    global _worker
    with _state_lock:
        if _state["running"]:
            return False, "已有重试任务正在运行"
        _state.update(
            running=True,
            success=False,
            attempts=0,
            lastStatus=None,
            startedAt=time.time(),
            finishedAt=None,
            nextAttemptAt=time.time(),
            message="正在请求",
            logs=[],
        )
        _stop_event.clear()
        _worker = threading.Thread(target=_retry_loop, args=(code, interval), daemon=True)
        _worker.start()
    return True, "started"


class Handler(BaseHTTPRequestHandler):
    server_version = "RedeemRetry/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def do_GET(self) -> None:
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        if self.path == "/":
            data = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/state":
            self._send_json(_public_state())
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/api/start":
                code = str(payload.get("code") or "").strip()
                if not code:
                    self._send_json({"error": "兑换码不能为空"}, 400)
                    return
                interval = max(MIN_INTERVAL_SECONDS, min(300.0, float(payload.get("interval", 1))))
                ok, message = _start(code, interval)
                if not ok:
                    self._send_json({"error": message}, 409)
                    return
                self._send_json(_public_state())
                return
            if self.path == "/api/stop":
                _stop_event.set()
                with _state_lock:
                    if _state["running"]:
                        _state["message"] = "正在停止"
                self._send_json(_public_state())
                return
            self._send_json({"error": "not found"}, 404)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, 400)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Redeem 请求重试器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Redeem 请求重试器已启动: {url}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
