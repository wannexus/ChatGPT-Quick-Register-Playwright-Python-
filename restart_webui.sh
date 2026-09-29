#!/usr/bin/env bash
# 重启 ChatGPT Quick Register 的 Web UI（脱离会话运行，关闭终端/关闭对话后仍在）
#
# 用法：
#   ./restart_webui.sh [端口]        # 默认 8765（可用 QR_WEB_PORT 覆盖）
#   ./restart_webui.sh --stop        # 只停止
#   ./restart_webui.sh --force       # 有批次在跑时也强制重启
#
# 与 start_webui.sh 的区别：start_webui.sh 是前台运行（exec，关终端即退出）；
# 本脚本用 setsid(2) 起新会话 + 新进程组，stdout/stderr 落 output/webui.log，
# PID 落 output/webui.pid，因此不受当前 shell / 会话 / 对话结束影响。
#
# 注意：页面开着时浏览器会挂一条 SSE 长连接（/api/batch/stream），uvicorn 的优雅退出会
# 一直等它关闭，所以旧实例收不到 SIGTERM 后会被 SIGKILL 结束；正在跑的批次请等它结束。
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="$(command -v python3)"
fi

LOG="output/webui.log"
PIDFILE="output/webui.pid"

PORT=""
STOP=""
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --stop) STOP=1 ;;
    --force|-f) FORCE=1 ;;
    *) PORT="$arg" ;;
  esac
done
PORT="${PORT:-${QR_WEB_PORT:-8765}}"
HOST="${QR_WEB_HOST:-127.0.0.1}"

stop_instance() {
  local pids=()
  if [ -f "$PIDFILE" ]; then
    local saved
    saved="$(tr -dc '0-9' < "$PIDFILE" || true)"
    [ -n "$saved" ] && pids+=("$saved")
  fi
  # 兜底：占用端口的进程
  if command -v lsof >/dev/null 2>&1; then
    while IFS= read -r pid; do
      [ -n "$pid" ] && pids+=("$pid")
    done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi

  local alive=()
  local seen=" "
  for pid in "${pids[@]:-}"; do
    [ -n "${pid:-}" ] || continue
    case "$seen" in *" $pid "*) continue ;; esac   # pidfile 与 lsof 可能给出同一个 PID
    seen="$seen$pid "
    if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q "webui/server.py"; then
      alive+=("$pid")
    fi
  done
  if [ "${#alive[@]}" -eq 0 ]; then
    echo "[restart] 没有正在运行的实例"
    return 0
  fi

  echo "[restart] 停止旧实例：${alive[*]}"
  kill -TERM "${alive[@]}" 2>/dev/null || true
  # 3 秒宽限；浏览器日志流会阻塞 uvicorn 的优雅退出，超时后强杀
  for _ in $(seq 1 6); do
    local still=()
    for pid in "${alive[@]}"; do kill -0 "$pid" 2>/dev/null && still+=("$pid"); done
    [ "${#still[@]}" -eq 0 ] && break
    sleep 0.5
  done
  local killed=()
  for pid in "${alive[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      killed+=("$pid")
    fi
  done
  [ "${#killed[@]}" -gt 0 ] && echo "[restart] 旧实例 ${killed[*]} 处于长连接中（页面 SSE 日志流），已强制结束"
}

if [ -n "$STOP" ]; then
  stop_instance
  rm -f "$PIDFILE"
  echo "[restart] 已停止"
  exit 0
fi

# 批次正在跑时默认不重启：重启会让该批次的输出流断开
if [ -z "$FORCE" ]; then
  status="$(curl -fsS --max-time 3 "http://${HOST}:${PORT}/api/batch/status" 2>/dev/null || true)"
  case "$status" in
    *'"running":true'*)
      echo "[restart] ✗ 有批次正在运行（$status）：等它跑完，或加 --force 强制重启"
      exit 1
      ;;
  esac
fi

stop_instance
mkdir -p "$(dirname "$LOG")"

# 脱离会话启动：start_new_session=True → setsid，新会话 + 新进程组，PPID 变 1
QR_WEB_HOST="$HOST" QR_WEB_PORT="$PORT" "$PY" - "$LOG" "$PIDFILE" <<'PY'
import os
import subprocess
import sys

log, pidfile = sys.argv[1], sys.argv[2]
handle = open(log, "ab", buffering=0)
proc = subprocess.Popen(
    [sys.executable, "webui/server.py"],
    stdin=subprocess.DEVNULL,
    stdout=handle,
    stderr=handle,
    start_new_session=True,   # setsid：不受当前终端/会话/对话结束影响
    close_fds=True,
)
with open(pidfile, "w") as fp:
    fp.write(str(proc.pid))
print(f"[restart] 已分离启动 pid={proc.pid} log={log}")
PY

# 等待就绪
ok=""
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://${HOST}:${PORT}/" >/dev/null 2>&1; then ok=1; break; fi
  sleep 0.5
done

if [ -z "$ok" ]; then
  echo "[restart] ✗ 启动失败，最后 30 行日志："
  tail -30 "$LOG" || true
  exit 1
fi

PID="$(tr -dc '0-9' < "$PIDFILE")"
echo "[restart] ✓ Web UI 就绪：http://${HOST}:${PORT}/"
ps -o pid=,ppid=,sess=,tty=,command= -p "$PID" || true
echo "[restart] 日志：$LOG  |  停止：./restart_webui.sh --stop"
