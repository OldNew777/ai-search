#!/usr/bin/env python3
"""AI Search pipeline manager - cross-platform, standard library only.

Layout (data root = the directory that contains `skills/`):

    <data-root>/
      skills/ai-search/           <- this skill (SKILL.md, scripts/, config/)
      searxng/core-config/        <- SearXNG settings mounted into the container
      fallback-search/            <- local MCP gateway (fallback chain)
      scrapling/.venv/            <- Scrapling + Chromium
      open-websearch/             <- cloned upstream (built with npm)
      mcp-searxng/                <- npm install of mcp-searxng
      logs/, _backup/, .env

Commands (see `ai_search.py <cmd> -h`):
    start            start podman machine + container + SSH tunnel (idempotent, safe to auto-run)
    stop             IMMEDIATE stop - only run this when the user explicitly asks for it
    idle-check       used by the OS scheduler: stop only if idle for N minutes
    status/verify    health checks
    install-idle-task / uninstall-idle-task
    sync-agent-config   regenerate Codex MCP config + AGENTS.md/CLAUDE.md blocks
    link-skill          link this skill into ~/.codex/skills and ~/.claude/skills
    bootstrap           one-shot setup on a fresh machine
    repair              after moving the data root
    rollback            restore the last backed-up agent config
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

SKILL_DIR = Path(__file__).resolve().parent.parent          # <data-root>/skills/ai-search
DATA_ROOT = SKILL_DIR.parent.parent                          # <data-root>
CONFIG_PATH = SKILL_DIR / "config" / "settings.json"

AGENT_BLOCK_START = "<!-- ai-search-pipeline:start -->"
AGENT_BLOCK_END = "<!-- ai-search-pipeline:end -->"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


CFG = load_config()
PORTS = CFG["ports"]
CONTAINER = CFG["container"]
PATHS = CFG["paths"]


def path_of(key: str) -> Path:
    return DATA_ROOT / PATHS[key]


def log_dir() -> Path:
    d = path_of("logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- helpers

def run(cmd, check=False, cwd=None, timeout=300, env=None, stdin_data=None):
    """Run a command, returning (returncode, stdout, stderr).

    stdin_data (str) is fed to the process on stdin - used by the egress probe.
    """
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, env=env, errors="replace", input=stdin_data,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def find_podman():
    exe = which("podman")
    if exe:
        return exe
    cands = []
    if IS_WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        lad = os.environ.get("LOCALAPPDATA", "")
        cands += [Path(pf) / "RedHat" / "Podman" / "podman.exe",
                  Path(lad) / "Programs" / "Podman" / "podman.exe"]
    elif IS_MACOS:
        cands += [Path("/opt/homebrew/bin/podman"), Path("/usr/local/bin/podman")]
    else:
        cands += [Path("/usr/bin/podman"), Path("/usr/local/bin/podman")]
    cands.append(Path.home() / ".local" / "bin" / ("podman.exe" if IS_WINDOWS else "podman"))
    for c in cands:
        if c and c.exists():
            return str(c)
    return None


def find_ssh():
    exe = which("ssh")
    if exe:
        return exe
    if IS_WINDOWS:
        cand = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if cand.exists():
            return str(cand)
    return None


def find_python():
    for name in ("python3", "python", "py"):
        exe = which(name)
        if exe:
            return exe
    return sys.executable


def find_pythonw():
    if not IS_WINDOWS:
        return find_python()
    exe = which("pythonw")
    if exe:
        return exe
    cand = Path(sys.executable).with_name("pythonw.exe")
    return str(cand) if cand.exists() else find_python()


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def http_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-search/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def searxng_ready(timeout: float = 6.0) -> bool:
    try:
        data = http_json(f"http://127.0.0.1:{PORTS['searxng']}/search?q=ping&format=json", timeout)
        return isinstance(data, dict) and "results" in data
    except Exception:
        return False


def read_env_file() -> dict:
    env_path = path_of("envFile")
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def ensure_secret() -> str:
    env_path = path_of("envFile")
    values = read_env_file()
    secret = values.get("SEARXNG_SECRET")
    if not secret:
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        values["SEARXNG_SECRET"] = secret
        body = "# AI-search environment (never commit this file)\n" + "\n".join(
            f"{k}={v}" for k, v in values.items()) + "\n"
        env_path.write_text(body, encoding="utf-8")
    return secret


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if IS_WINDOWS:
            rc, out, _ = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=20)
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def pid_file() -> Path:
    return log_dir() / "ssh-tunnel.pid"


def marker_file() -> Path:
    return log_dir() / "last-search.marker"


def touch_marker() -> None:
    marker_file().write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")


# --------------------------------------------------------------------------- podman

def machine_name(podman: str) -> str:
    rc, out, _ = run([podman, "machine", "list", "--format", "{{.Name}}"], timeout=60)
    names = [n.strip() for n in out.splitlines() if n.strip()]
    return names[0] if names else "podman-machine-default"


def machine_running(podman: str) -> bool:
    rc, out, _ = run([podman, "info", "--format", "{{.Host.OS}}"], timeout=60)
    return rc == 0 and "linux" in out


def machine_ssh_config(podman: str, name: str) -> dict:
    info = {"port": 22, "identity": None, "user": "root"}
    rc, out, _ = run([podman, "machine", "inspect", name], timeout=60)
    if rc != 0:
        return info
    try:
        data = json.loads(out)
        data = data[0] if isinstance(data, list) else data
        ssh = data.get("SSHConfig") or {}
        if ssh.get("Port"):
            info["port"] = int(ssh["Port"])
        if ssh.get("IdentityPath"):
            info["identity"] = ssh["IdentityPath"]
        rootful = bool(data.get("Rootful"))
        info["user"] = "root" if rootful else (ssh.get("RemoteUsername") or "root")
    except Exception:
        pass
    return info


def podman_capture(podman: str, *args: str, timeout: int = 120):
    return run([podman, *args], timeout=timeout)


def container_exists(podman: str) -> bool:
    rc, out, _ = podman_capture(podman, "ps", "-a", "--format", "{{.Names}}", timeout=60)
    return CONTAINER["name"] in [n.strip() for n in out.splitlines()]


def container_running(podman: str) -> bool:
    rc, out, _ = podman_capture(podman, "inspect", "--format", "{{.State.Running}}", CONTAINER["name"], timeout=60)
    return out.strip().lower() == "true"


def container_ip(podman: str) -> str:
    rc, out, _ = podman_capture(podman, "inspect", "--format", "{{.NetworkSettings.IPAddress}}", CONTAINER["name"], timeout=60)
    return out.strip()


# --------------------------------------------------------------------------- network preflight

# The probe below runs *inside* the container on purpose: the SearXNG web app can answer on
# 127.0.0.1 while the engines behind it have no DNS and no route out (this is what happens on
# Windows when the WSL-based podman machine loses its user-mode networking). Only a probe from
# inside the container tells the truth about whether searching can actually work.
EGRESS_PROBE = """\
import socket

hosts = ("www.bing.com", "www.baidu.com")
resolved, connected, errors = [], [], []
for host in hosts:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        resolved.append(host)
    except Exception as exc:
        errors.append("dns:%s:%s" % (host, type(exc).__name__))
        continue
    try:
        socket.create_connection((host, 443), 4).close()
        connected.append(host)
    except Exception as exc:
        errors.append("tcp:%s:%s" % (host, type(exc).__name__))

if connected:
    print("PROBE=OK connected=%s resolved=%s" % (",".join(connected), ",".join(resolved)))
elif resolved:
    print("PROBE=FAIL no-tcp resolved=%s errors=%s" % (",".join(resolved), ";".join(errors)))
else:
    print("PROBE=FAIL no-dns errors=%s" % ";".join(errors))
"""


def container_egress(podman: str, timeout: int = 60):
    """Probe DNS + TCP egress from inside the container.

    (True, detail)  - the container resolves names and reaches the internet
    (False, detail) - it demonstrably cannot (all engines will return nothing)
    (None, detail)  - undecidable (no podman/container/python)
    """
    if not podman:
        return None, "podman not found"
    if not container_running(podman):
        return None, "container is not running"
    last = "no python interpreter inside the container image"
    for interpreter in ("python", "python3"):
        rc, out, err = run([podman, "exec", "-i", CONTAINER["name"], interpreter, "-"],
                           timeout=timeout, stdin_data=EGRESS_PROBE)
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip().startswith("PROBE=")]
        if lines:
            return lines[-1].startswith("PROBE=OK"), lines[-1].replace("PROBE=", "", 1)
        blob = f"{err or ''}\n{out or ''}".lower()
        if "executable file" in blob or "no such file" in blob or "not found" in blob:
            continue
        text = (err or out or f"probe failed (rc={rc})").strip()
        if text:
            last = text.splitlines()[-1][:200]
        break
    return None, last


def foreign_running_containers(podman: str):
    """Running containers other than ours - None when the list cannot be read."""
    rc, out, _ = podman_capture(podman, "ps", "--format", "{{.Names}}", timeout=60)
    if rc != 0:
        return None
    return [n.strip() for n in out.splitlines() if n.strip() and n.strip() != CONTAINER["name"]]


def heal_network(podman: str) -> bool:
    """Restart the podman machine to restore container egress, then rebuild container + tunnel.

    Safety gate: if any other container is running on that machine the machine is left alone,
    so a repair can never disturb unrelated workloads.
    """
    if not podman:
        return False
    others = foreign_running_containers(podman)
    if others is None:
        print("  ! cannot list the containers - not restarting the podman machine on my own")
        return False
    if others:
        print(f"  ! other containers are running ({', '.join(others)}) - "
              "not restarting the podman machine")
        return False
    name = machine_name(podman)
    print(f"  restarting podman machine '{name}' (container egress is gone) ...")
    rc, out, err = podman_capture(podman, "machine", "stop", timeout=600)
    if rc != 0:
        print(f"  machine stop returned {rc}: {(err or out).strip()[:160]}")
    rc, out, err = podman_capture(podman, "machine", "start", timeout=900)
    if rc != 0:
        print(f"  ! machine start failed: {(err or out).strip()[:240]}")
        return False
    for _ in range(24):
        if machine_running(podman):
            break
        time.sleep(5)
    else:
        print("  ! the machine did not come back")
        return False
    try:
        cip = ensure_container(podman)
    except Exception as exc:
        print(f"  ! the container did not come back: {exc}")
        return False
    if not start_tunnel(podman, cip):
        print("  ! the tunnel did not come back")
        return False
    for _ in range(20):
        if searxng_ready():
            return True
        time.sleep(3)
    print("  ! SearXNG did not answer after the machine restart")
    return False


def check_egress(podman: str, heal: bool = True):
    """Preflight the container's outbound network, repairing it once when it is broken.

    True  - engines can reach the internet
    False - they cannot: use the fallback chain and tell the user
    None  - could not be determined (do not draw conclusions)
    """
    ok, detail = container_egress(podman)
    if ok is False:
        # a machine that has just been started can need a few seconds for route + DNS
        time.sleep(5)
        ok, detail = container_egress(podman)
    if ok:
        print(f"[OK] container egress: {detail}")
        return True
    if ok is None:
        print(f"[INFO] container egress unknown: {detail}")
        return None
    print(f"[WARN] SearXNG has no outbound network - {detail}")
    if heal and heal_network(podman):
        ok, detail = container_egress(podman)
        if ok:
            print(f"[OK] container egress restored: {detail}")
            return True
        print(f"[WARN] the container still has no outbound network - {detail}")
    print("[DEGRADED] SearXNG is up but its engines cannot reach the network: use fallback_search "
          "and tell the user the primary path is down.")
    return False





def ensure_container(podman: str) -> str:
    secret = ensure_secret()
    publish = f"127.0.0.1:{PORTS['searxng']}:{PORTS['container']}"
    config_dir = path_of("searxngConfig")
    if not container_exists(podman):
        cmd = [
            podman, "run", "-d", "--name", CONTAINER["name"], "--restart", "unless-stopped",
            "-p", publish,
            "-v", f"{config_dir}:/etc/searxng:rw",
            "-e", f"SEARXNG_BASE_URL=http://127.0.0.1:{PORTS['searxng']}/",
            "-e", f"SEARXNG_SECRET={secret}",
            CONTAINER["image"],
        ]
        rc, out, err = run(cmd, timeout=600)
        if rc != 0:
            raise RuntimeError(f"podman run failed: {err.strip()}")
        print(f"  created container {CONTAINER['name']}")
    elif not container_running(podman):
        rc, out, err = podman_capture(podman, "start", CONTAINER["name"], timeout=300)
        if rc != 0:
            raise RuntimeError(f"podman start failed: {err.strip()}")
        print(f"  started container {CONTAINER['name']}")

    ip = ""
    for _ in range(20):
        time.sleep(3)
        if container_running(podman):
            ip = container_ip(podman)
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                return ip
    raise RuntimeError(f"container did not become ready (ip={ip!r})")


def stop_tunnel(quiet: bool = False) -> None:
    pf = pid_file()
    if not pf.exists():
        return
    try:
        pid = int(pf.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        pid = 0
    if pid and pid_alive(pid):
        try:
            if IS_WINDOWS:
                run(["taskkill", "/PID", str(pid), "/F", "/T"], timeout=30)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
        if not quiet:
            print(f"  stopped SSH tunnel (pid {pid})")
    pf.unlink(missing_ok=True)


def start_tunnel(podman: str, cip: str) -> bool:
    ssh = find_ssh()
    if not ssh:
        print("  ! ssh client not found - cannot create the local tunnel")
        return False
    stop_tunnel(quiet=True)
    # free the port if a stale ssh tunnel from a previous run still holds it
    if port_open(PORTS["searxng"]):
        time.sleep(1)
    name = machine_name(podman)
    info = machine_ssh_config(podman, name)
    known_hosts = "NUL" if IS_WINDOWS else "/dev/null"
    argv = [ssh]
    if info.get("identity"):
        argv += ["-i", info["identity"]]
    argv += [
        "-p", str(info["port"]),
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
        "-L", f"127.0.0.1:{PORTS['searxng']}:{cip}:{PORTS['container']}",
        f"{info['user']}@127.0.0.1",
    ]
    out_log = log_dir() / "ssh-tunnel.out.log"
    err_log = log_dir() / "ssh-tunnel.err.log"
    stdout = out_log.open("w", encoding="utf-8")
    stderr = err_log.open("w", encoding="utf-8")
    kwargs = {"stdout": stdout, "stderr": stderr, "stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    pid_file().write_text(str(proc.pid), encoding="utf-8")
    for _ in range(15):
        time.sleep(1)
        if port_open(PORTS["searxng"]):
            print(f"  tunnel up (pid {proc.pid}) -> {cip}:{PORTS['container']}")
            return True
    msg = ""
    try:
        msg = err_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    except Exception:
        pass
    print(f"  ! tunnel did not come up{': ' + msg if msg else ''}")
    return False


# --------------------------------------------------------------------------- commands

def cmd_start(args) -> int:
    touch_marker()
    with_daemon = bool(getattr(args, "with_daemon", False))
    quick = bool(getattr(args, "quick", False))
    auto_heal = not bool(getattr(args, "no_heal", False))
    podman = find_podman()

    if searxng_ready():
        print(f"[SearXNG] already ready on 127.0.0.1:{PORTS['searxng']}")
    else:
        if not podman:
            print("! podman not found - install Podman Desktop (with the Podman and Compose extensions)")
            return 1

        if not machine_running(podman):
            print("[1/4] starting the podman machine ...")
            rc, out, err = run([podman, "machine", "start"], timeout=900)
            for _ in range(24):
                if machine_running(podman):
                    break
                time.sleep(5)
            else:
                print(f"! machine did not start: {(err or out).strip()[:300]}")
                return 1

        print("[2/4] ensuring the SearXNG container ...")
        cip = ensure_container(podman)
        print(f"  container ip = {cip}")

        print(f"[3/4] opening the SSH tunnel 127.0.0.1:{PORTS['searxng']} -> {cip}:{PORTS['container']} ...")
        tunnel_ok = start_tunnel(podman, cip)

        if not tunnel_ok and IS_LINUX:
            # native podman publishes ports directly; no tunnel needed
            print("  (native podman: relying on the published port)")

        print("[4/4] waiting for SearXNG ...")
        ready = False
        for _ in range(20):
            time.sleep(3)
            if searxng_ready():
                ready = True
                break
        if not ready:
            print("! SearXNG did not answer - run `status` for details")
            return 1
        print("[OK] SearXNG is ready")

    if not quick:
        # An HTTP 200 from SearXNG only proves the web app is up, not that its engines can
        # reach the network - that gap is exactly what produced silent 0-result searches.
        check_egress(podman, heal=auto_heal)

    if with_daemon:
        start_daemon()
    return 0


def cmd_stop(args) -> int:
    """IMMEDIATE stop - only run when the user explicitly asks for it."""
    podman = find_podman()
    stop_tunnel()
    if podman:
        if container_exists(podman):
            print(f"  stopping container {CONTAINER['name']}")
            podman_capture(podman, "stop", CONTAINER["name"], timeout=180)
        if getattr(args, "all", False):
            print("  stopping the podman machine")
            podman_capture(podman, "machine", "stop", timeout=300)
        stop_daemon()
    print("[OK] search service stopped")
    return 0


def cmd_idle_check(args) -> int:
    minutes = args.minutes or CFG["idle"]["minutes"]
    marker = marker_file()
    if marker.exists():
        idle = time.time() - marker.stat().st_mtime
    else:
        idle = float("inf")
    if idle < minutes * 60:
        print(f"[idle-check] idle {idle/60:.1f} min < {minutes} min - keep running")
        return 0
    print(f"[idle-check] idle {idle/60:.0f} min >= {minutes} min - stopping")
    return cmd_stop(argparse.Namespace(all=args.stop_machine, minutes=minutes))


def cmd_status(args) -> int:
    podman = find_podman()
    print(f"data root : {DATA_ROOT}")
    print(f"skill dir : {SKILL_DIR}")
    print("--- ports ---")
    print(f"  {'[OK]  ' if port_open(PORTS['searxng']) else '[DOWN]'} searxng tunnel  127.0.0.1:{PORTS['searxng']}")
    print(f"  {'[OK]  ' if port_open(PORTS['daemon']) else '[----]'} open-websearch daemon 127.0.0.1:{PORTS['daemon']} (optional)")
    print("--- health ---")
    if searxng_ready(10):
        try:
            data = http_json(f"http://127.0.0.1:{PORTS['searxng']}/search?q=ping&format=json", 30)
            bad = ", ".join(e[0] for e in (data.get("unresponsive_engines") or []))
            print(f"  [OK]   searxng answered with {len(data.get('results') or [])} results (unresponsive engines: {bad or 'none'})")
        except Exception as exc:
            print(f"  [OK]   searxng reachable ({exc})")
    else:
        print("  [DOWN] searxng not reachable")
    pf = pid_file()
    if pf.exists():
        pid = int((pf.read_text(encoding="utf-8").strip() or "0"))
        print(f"  [INFO] ssh tunnel pid {pid} alive={pid_alive(pid)}")
    if podman:
        rc, out, _ = podman_capture(podman, "ps", "--filter", f"name={CONTAINER['name']}",
                                    "--format", "{{.Names}}  {{.Status}}  {{.Ports}}", timeout=60)
        print("--- container ---")
        print("  " + (out.strip().replace("\n", "\n  ") or "(not running)"))
    else:
        print("--- container ---\n  podman not found")
    if podman and container_running(podman):
        ok, detail = container_egress(podman)
        label = "[OK]  " if ok else ("[????]" if ok is None else "[FAIL]")
        print("--- egress ---")
        print(f"  {label} container -> internet: {detail}")
        others = foreign_running_containers(podman)
        if others:
            print(f"  [INFO] other containers on this machine: {', '.join(others)}")
    return 0


def cmd_verify(args) -> int:
    if cmd_start(argparse.Namespace(with_daemon=False)) != 0:
        print("[FAIL] service could not be started")
        return 1
    print(f"[OK] data root : {DATA_ROOT}")
    print(f"[OK] skill dir : {SKILL_DIR}")
    try:
        data = http_json(f"http://127.0.0.1:{PORTS['searxng']}/search?q=ai-search%20selfcheck&format=json", 60)
    except Exception as exc:
        print(f"[FAIL] searxng   : the self-check query failed ({exc})")
        return 1
    results = data.get("results") or []
    bad = ", ".join(e[0] for e in (data.get("unresponsive_engines") or []))
    if not results:
        print(f"[FAIL] searxng   : 0 results (unresponsive engines: {bad or 'none'})")
        print("[FAIL] the local pipeline cannot search right now - use the fallback chain and tell the user")
        return 1
    suffix = f" (unresponsive engines: {bad})" if bad else ""
    print(f"[OK] searxng   : {len(results)} results on 127.0.0.1:{PORTS['searxng']}{suffix}")
    return 0


def cmd_heal(args) -> int:
    """Bring the service up and repair the podman machine network when the container is cut off."""
    if cmd_start(argparse.Namespace(with_daemon=False)) != 0:
        print("[FAIL] service could not be started")
        return 1
    ok, detail = container_egress(find_podman())
    if ok:
        print(f"[OK] container egress is healthy: {detail}")
        return 0
    state = "unknown" if ok is None else "still broken"
    print(f"[FAIL] container egress {state}: {detail}")
    return 1


# --------------------------------------------------------------------------- daemon (optional)

def daemon_ready() -> bool:
    try:
        http_json(f"http://127.0.0.1:{PORTS['daemon']}/health", 3)
        return True
    except Exception:
        return False


def start_daemon() -> None:
    if daemon_ready():
        print(f"  [open-websearch] daemon already on 127.0.0.1:{PORTS['daemon']}")
        return
    node = which("node")
    workdir = path_of("openWebSearch")
    if not node or not (workdir / "build" / "index.js").exists():
        print("  [open-websearch] not installed - run `bootstrap` first")
        return
    logs = log_dir()
    out = (logs / "open-websearch.out.log").open("w", encoding="utf-8")
    err = (logs / "open-websearch.err.log").open("w", encoding="utf-8")
    kwargs = {"cwd": str(workdir), "stdout": out, "stderr": err, "stdin": subprocess.DEVNULL}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([node, "build/index.js", "serve", "--port", str(PORTS["daemon"])], **kwargs)
    for _ in range(10):
        time.sleep(1)
        if daemon_ready():
            print(f"  [open-websearch] daemon started on 127.0.0.1:{PORTS['daemon']}")
            return
    print("  [open-websearch] daemon did not answer")


def _listening_pids(port: int):
    pids = []
    if IS_WINDOWS:
        rc, out, _ = run(["netstat", "-ano", "-p", "TCP"], timeout=60)
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                pids.append(int(line.split()[-1]))
    else:
        exe = which("lsof")
        if exe:
            rc, out, _ = run([exe, "-ti", f"tcp:{port}", "-sTCP:LISTEN"], timeout=60)
            pids = [int(x) for x in out.split() if x.strip().isdigit()]
    return sorted(set(pids))


def stop_daemon() -> None:
    for pid in _listening_pids(PORTS["daemon"]):
        try:
            if IS_WINDOWS:
                run(["taskkill", "/PID", str(pid), "/F"], timeout=30)
            else:
                os.kill(pid, 15)
            print(f"  stopped open-websearch daemon (pid {pid})")
        except Exception:
            pass


# --------------------------------------------------------------------------- OS scheduler

TASK_NAME = "AI-Search Idle Stop"


def _task_command() -> list:
    return [find_pythonw(), str(Path(__file__).resolve()), "idle-check"]


def install_idle_task(minutes: int) -> int:
    cmd = _task_command()
    if IS_WINDOWS:
        tr = subprocess.list2cmdline(cmd)
        rc, out, err = run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
                            "/SC", "MINUTE", "/MO", str(minutes), "/F"], timeout=120)
        if rc != 0:
            print(f"! schtasks failed: {err.strip() or out.strip()}")
            return 1
        print(f"[OK] scheduled task '{TASK_NAME}' registered (every {minutes} min)")
    elif IS_MACOS:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.ai-search.idle.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ai-search.idle</string>
  <key>ProgramArguments</key><array>{''.join(f'<string>{a}</string>' for a in cmd)}</array>
  <key>StartInterval</key><integer>{minutes * 60}</integer>
</dict></plist>
""", encoding="utf-8")
        run(["launchctl", "unload", str(plist)], timeout=60)
        rc, out, err = run(["launchctl", "load", str(plist)], timeout=60)
        if rc != 0:
            print(f"! launchctl failed: {err.strip()}")
            return 1
        print(f"[OK] launchd agent installed (every {minutes} min)")
    else:
        exe = which("crontab")
        if not exe:
            print("! crontab not found; add this line manually:")
            print(f"  */{minutes} * * * * {' '.join(cmd)} >> {log_dir()/'idle-check.log'} 2>&1")
            return 1
        rc, out, _ = run([exe, "-l"], timeout=60)
        lines = [l for l in out.splitlines() if "ai-search.py" not in l and "ai_search.py" not in l]
        lines.append(f"*/{minutes} * * * * {' '.join(cmd)} >> {log_dir()/'idle-check.log'} 2>&1")
        proc = subprocess.run([exe, "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
        if proc.returncode != 0:
            print(f"! crontab update failed: {proc.stderr.strip()}")
            return 1
        print(f"[OK] cron entry installed (every {minutes} min)")
    return 0


def uninstall_idle_task() -> int:
    if IS_WINDOWS:
        rc, out, err = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=120)
        print("[OK] scheduled task removed" if rc == 0 else f"! {err.strip() or out.strip()}")
        return rc
    if IS_MACOS:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.ai-search.idle.plist"
        if plist.exists():
            run(["launchctl", "unload", str(plist)], timeout=60)
            plist.unlink()
        print("[OK] launchd agent removed")
        return 0
    exe = which("crontab")
    if exe:
        rc, out, _ = run([exe, "-l"], timeout=60)
        lines = [l for l in out.splitlines() if "ai_search.py" not in l]
        subprocess.run([exe, "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
        print("[OK] cron entry removed")
    return 0


# --------------------------------------------------------------------------- agent config

AGENT_BLOCK = """<!-- ai-search-pipeline:start -->
# Web search and crawling (use the `ai-search` skill)

For any task that needs live web information - searching the web, reading or crawling a URL,
checking current versions, changelogs, release notes or issue threads - use the **ai-search** skill.
Do not hardcode any directory: locate the skill in your own skills directory (the skill is named
`ai-search`) and follow its SKILL.md for the exact commands.

Rules of engagement (full details live in the skill):

- **Start the local search service before the first web request.** The skill ships an idempotent
  start command; running it automatically is expected and approved.
- **Prefer the primary path**: SearXNG for search, Scrapling for fetching, and the skill's built-in
  fallback chain (open-webSearch -> Firecrawl -> DuckDuckGo) only when the primary path fails.
- **Do not trust "the service is up".** SearXNG answering on its port only means the web app is
  running - not that its engines can reach the network. If a search comes back empty or every engine
  times out, run the skill's `verify` (it repairs the container network, then re-runs a real query);
  if it still fails, fall back to `fallback_search` and tell the user the primary path is down.
- **Never stop the service on your own initiative.** A finished search or an idle moment is not a
  reason to shut anything down. Stop it only when the user explicitly asks for it, and then use the
  skill's immediate `stop` command. The single exception is the skill's own egress self-heal inside
  `start`/`heal`: it restarts the podman machine only when the container has no working network and
  no other containers are running, and it brings the service straight back.
- The service may stop itself when idle (OS scheduler entry installed by the skill). That is
  expected - just run the skill's start command again before the next search.
<!-- ai-search-pipeline:end -->"""


def _mcp_block() -> str:
    scrapling = path_of("scraplingVenv") / "Scripts" / "scrapling-mcp.exe"
    if not IS_WINDOWS:
        scrapling = path_of("scraplingVenv") / "bin" / "scrapling-mcp"
    mcp_searx = path_of("mcpSearxng") / "node_modules" / "mcp-searxng" / "dist" / "cli.js"
    open_web = path_of("openWebSearch") / "build" / "index.js"
    fallback = path_of("fallbackVenv") / "Scripts" / "fallback-search.exe"
    if not IS_WINDOWS:
        fallback = path_of("fallbackVenv") / "bin" / "fallback-search"
    return f"""# ===========================================================================
# AI-search pipeline (generated by the ai-search skill: scripts/ai_search.py sync-agent-config)
# data root: {DATA_ROOT}
# generated: {time.strftime('%Y-%m-%d %H:%M:%S')}
# ===========================================================================

[mcp_servers.scrapling]
command = '{scrapling}'
startup_timeout_sec = 120
tool_timeout_sec = 180
enabled_tools = ["make_request", "fetch", "stealthy_fetch", "bulk_fetch", "open_session", "session_fetch", "list_sessions", "close_session", "screenshot"]

[mcp_servers.searxng]
command = "node"
args = ['{mcp_searx}']
startup_timeout_sec = 90
tool_timeout_sec = 90

[mcp_servers.searxng.env]
SEARXNG_URL = "http://127.0.0.1:{PORTS['searxng']}"

[mcp_servers.open_websearch]
command = "node"
args = ['{open_web}']
startup_timeout_sec = 90
tool_timeout_sec = 120

[mcp_servers.open_websearch.env]
MODE = "stdio"

[mcp_servers.firecrawl]
url = "https://mcp.firecrawl.dev/v2/mcp"
startup_timeout_sec = 90
tool_timeout_sec = 120

[mcp_servers.duckduckgo]
command = "uvx"
args = ["duckduckgo-mcp-server"]
startup_timeout_sec = 120
tool_timeout_sec = 90

[mcp_servers.fallback_search]
command = '{fallback}'
startup_timeout_sec = 90
tool_timeout_sec = 180

[mcp_servers.fallback_search.env]
OPEN_WEBSEARCH_URL = "http://127.0.0.1:{PORTS['daemon']}"
FIRECRAWL_MCP_URL = "https://mcp.firecrawl.dev/v2/mcp"
FALLBACK_PROVIDER_TIMEOUT = "25"
"""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _backup(paths) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = DATA_ROOT / "_backup" / f"sync-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p and p.exists():
            shutil.copy2(p, dest / p.name)
    return dest


def cmd_sync_agent_config(args) -> int:
    codex_home = Path.home() / ".codex"
    cfg_path = codex_home / "config.toml"
    agent_files = [codex_home / "AGENTS.md", Path.home() / ".claude" / "CLAUDE.md"]

    dest = _backup([cfg_path, *agent_files])
    print(f"[1/3] backup -> {dest}")

    if cfg_path.exists():
        lines = cfg_path.read_text(encoding="utf-8").splitlines()
        keep = []
        for line in lines:
            if line.startswith("# AI-search pipeline"):
                keep = keep[:-1] if keep and keep[-1].startswith("# ==") else keep
                break
            keep.append(line)
        body = "\n".join(keep).rstrip() + "\n\n" + _mcp_block()
    else:
        body = _mcp_block()
    _write_text(cfg_path, body)
    print("[2/3] rewrote the AI-search block in config.toml")

    for af in agent_files:
        if af.exists():
            text = af.read_text(encoding="utf-8")
            if AGENT_BLOCK_START in text and AGENT_BLOCK_END in text:
                text = re.sub(re.escape(AGENT_BLOCK_START) + r".*?" + re.escape(AGENT_BLOCK_END),
                              AGENT_BLOCK, text, flags=re.S)
            else:
                text = text.rstrip() + "\n\n" + AGENT_BLOCK + "\n"
        else:
            text = AGENT_BLOCK + "\n"
        _write_text(af, text)
        print(f"[3/3] updated {af}")
    return 0


# --------------------------------------------------------------------------- skill links

def cmd_link_skill(args) -> int:
    links = {
        "codex": Path.home() / CFG["skillLinks"]["codex"],
        "claude": Path.home() / CFG["skillLinks"]["claude"],
    }
    for name, link in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if link.resolve() == SKILL_DIR.resolve():
                print(f"  [{name}] already linked: {link}")
                continue
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link, ignore_errors=True)
            else:
                link.unlink(missing_ok=True)
        if IS_WINDOWS:
            rc, out, err = run(["cmd", "/c", "mklink", "/J", str(link), str(SKILL_DIR)], timeout=60)
            ok = rc == 0
        else:
            try:
                os.symlink(SKILL_DIR, link, target_is_directory=True)
                ok = True
            except Exception as exc:
                ok = False
                err = str(exc)
        print(f"  [{name}] {link} -> {SKILL_DIR} {'OK' if ok else 'FAILED: ' + str(err)}")
    return 0


# --------------------------------------------------------------------------- bootstrap / repair

def _need(cmd: str) -> bool:
    return which(cmd) is None


def cmd_bootstrap(args) -> int:
    print(f"bootstrapping AI-search at {DATA_ROOT}")
    missing = [c for c in ("git", "node", "npm") if _need(c)]
    if not find_podman():
        missing.append("podman (Podman Desktop or the podman CLI)")
    if missing:
        print(f"! missing tools: {', '.join(missing)} - install them first")
        return 1
    uv = which("uv")
    py = find_python()

    if not args.skip_python:
        print("[1/6] scrapling environment ...")
        scrapling_venv = path_of("scraplingVenv")
        if uv:
            run([uv, "venv", "--python", CFG["upstream"]["pythonVersion"], str(scrapling_venv)], timeout=600)
            run([uv, "pip", "install", "--python", str(scrapling_venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")),
                 CFG["upstream"]["scraplingSpec"]], timeout=1800)
        else:
            run([py, "-m", "venv", str(scrapling_venv)], timeout=600)
            pip = scrapling_venv / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")
            run([str(pip), "install", CFG["upstream"]["scraplingSpec"]], timeout=1800)
        if not args.skip_browsers:
            exe = scrapling_venv / ("Scripts/scrapling.exe" if IS_WINDOWS else "bin/scrapling")
            print("      installing the browser runtime (large download) ...")
            run([str(exe), "install"], timeout=3600)

        print("[2/6] fallback gateway (editable) ...")
        fb_venv = path_of("fallbackVenv")
        if uv:
            run([uv, "venv", "--python", CFG["upstream"]["pythonVersion"], str(fb_venv)], timeout=600)
            run([uv, "pip", "install", "--python", str(fb_venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")),
                 "-e", str(path_of("fallbackSearch"))], timeout=900)
        else:
            run([py, "-m", "venv", str(fb_venv)], timeout=600)
            pip = fb_venv / ("Scripts/pip.exe" if IS_WINDOWS else "bin/pip")
            run([str(pip), "install", "-e", str(path_of("fallbackSearch"))], timeout=900)

    if not args.skip_npm:
        print("[3/6] open-websearch (clone + build) ...")
        ow = path_of("openWebSearch")
        if not (ow / "package.json").exists():
            run(["git", "clone", "--depth", "1", "--branch", CFG["upstream"]["openWebSearchRef"],
                 CFG["upstream"]["openWebSearchRepo"], str(ow)], timeout=900)
        run(["npm", "install", "--no-fund", "--no-audit"], cwd=ow, timeout=1800)
        run(["npm", "run", "build"], cwd=ow, timeout=900)

        print("[4/6] mcp-searxng (npm) ...")
        ms = path_of("mcpSearxng")
        ms.mkdir(parents=True, exist_ok=True)
        if not (ms / "package.json").exists():
            _write_text(ms / "package.json", '{ "name": "ai-search-mcp-searxng", "private": true }\n')
        run(["npm", "install", CFG["upstream"]["mcpSearxngNpm"], "--no-fund", "--no-audit"], cwd=ms, timeout=900)

    print("[5/6] secrets, links and agent config ...")
    ensure_secret()
    cmd_link_skill(args)
    cmd_sync_agent_config(args)
    minutes = args.minutes or CFG["idle"]["minutes"]
    install_idle_task(minutes)

    if not args.skip_start:
        print("[6/6] starting and verifying ...")
        rc = cmd_start(argparse.Namespace(with_daemon=False))
        if rc == 0:
            cmd_verify(argparse.Namespace())
    print("bootstrap finished")
    return 0


def cmd_repair(args) -> int:
    uv = which("uv")
    fb_venv = path_of("fallbackVenv")
    py = fb_venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    print("[1/4] reinstalling the fallback gateway (editable) ...")
    if uv:
        run([uv, "pip", "install", "--python", str(py), "-e", str(path_of("fallbackSearch"))], timeout=900)
    else:
        run([str(py), "-m", "pip", "install", "-e", str(path_of("fallbackSearch"))], timeout=900)
    print("[2/4] relinking the skill ...")
    cmd_link_skill(args)
    print("[3/4] syncing the agent config ...")
    cmd_sync_agent_config(args)
    print("[4/4] reinstalling the idle scheduler entry ...")
    install_idle_task(CFG["idle"]["minutes"])
    print("repair finished")
    return 0


def cmd_rollback(args) -> int:
    backups = sorted((DATA_ROOT / "_backup").glob("sync-*"))
    if not backups:
        print("! no backup found")
        return 1
    latest = backups[-1]
    mapping = {"codex-config.toml": Path.home() / ".codex" / "config.toml",
               "AGENTS.md": Path.home() / ".codex" / "AGENTS.md",
               "CLAUDE.md": Path.home() / ".claude" / "CLAUDE.md"}
    for name, target in mapping.items():
        src = latest / name
        if src.exists():
            shutil.copy2(src, target)
            print(f"  restored {target}")
    print(f"rolled back from {latest} - restart your agent to apply")
    return 0


# --------------------------------------------------------------------------- entry point

def main() -> int:
    parser = argparse.ArgumentParser(prog="ai_search", description="AI Search pipeline manager")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="start the search service (idempotent, safe to run automatically)")
    p.add_argument("--with-daemon", action="store_true", help="also start the optional open-websearch daemon")
    p.add_argument("--quick", action="store_true", help="skip the container egress preflight")
    p.add_argument("--no-heal", action="store_true",
                   help="never restart the podman machine automatically (the preflight still reports it)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="IMMEDIATE stop - only when the user explicitly asks for it")
    p.add_argument("--all", action="store_true", help="also stop the podman machine")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("idle-check", help="scheduler entry point: stop only if idle for N minutes")
    p.add_argument("--minutes", type=int, default=0)
    p.add_argument("--stop-machine", action="store_true")
    p.set_defaults(func=cmd_idle_check)

    sub.add_parser("status", help="show ports, health, tunnel and container state").set_defaults(func=cmd_status)
    sub.add_parser("verify", help="start and run a real query as a self-check").set_defaults(func=cmd_verify)
    sub.add_parser("heal", help="start + repair the machine network when the container has no egress").set_defaults(func=cmd_heal)

    p = sub.add_parser("install-idle-task", help="install the idle reclaimer (schtasks/cron/launchd)")
    p.add_argument("--minutes", type=int, default=0)
    p.set_defaults(func=lambda a: install_idle_task(a.minutes or CFG["idle"]["minutes"]))
    sub.add_parser("uninstall-idle-task", help="remove the idle reclaimer").set_defaults(func=lambda a: uninstall_idle_task())

    sub.add_parser("sync-agent-config", help="rewrite Codex MCP config + AGENTS.md/CLAUDE.md").set_defaults(func=cmd_sync_agent_config)
    sub.add_parser("link-skill", help="link this skill into the Codex/Claude skills directories").set_defaults(func=cmd_link_skill)

    p = sub.add_parser("bootstrap", help="one-shot setup on a fresh machine")
    p.add_argument("--skip-python", action="store_true")
    p.add_argument("--skip-browsers", action="store_true")
    p.add_argument("--skip-npm", action="store_true")
    p.add_argument("--skip-start", action="store_true")
    p.add_argument("--minutes", type=int, default=0)
    p.set_defaults(func=cmd_bootstrap)

    sub.add_parser("repair", help="after moving the data root: reinstall, relink, resync").set_defaults(func=cmd_repair)
    sub.add_parser("rollback", help="restore the last backed-up agent config").set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
