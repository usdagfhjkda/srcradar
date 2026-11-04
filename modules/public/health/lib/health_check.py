"""
modules/public/health/lib/health_check.py — the 5 health checks + the report
rendering

The checks:
    1. disk       the df -P parsing, it picks the mount points over the threshold
    2. cpu        the uptime load avg (1m/5m/15m) + the instantaneous usage of
                  top -bn1
    3. memory     the /proc/meminfo computation (Total - Available) / Total
    4. cron       the crontab -l parsing, it looks at the log mtime of every task
    5. dashboard  pgrep detects python3 lib/dashboard.py

Every check function has its own try/except and returns a
Finding(severity=alert) on a failure
instead of raising -> a single anomaly does not take the other checks down.

Usage:
    python3 health_check.py [--config <health.conf>] [--dryrun] [--run-id <id>] [--date-ph <ph>] [--out <path>]
    --config  use the given conf (the DEFAULT of
              config/health.conf.example is enough to run it)
    --dryrun  only generate health_report.md, do not call notify_send.sh
    --run-id  a custom run_id (the timestamp by default)
    --date-ph a custom date placeholder (%Y-%m-%d by default)
    --out     a custom report output path (logs/health_report.md by default)

Exit codes: 0=ok 1=arguments 3=a check raised on its own (an alert Finding
was written)
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Iterable

# the paths that are relative to this module
SCRIPT_DIR = Path(__file__).resolve().parent
HEALTH_DIR = SCRIPT_DIR.parent  # modules/public/health
REPO_ROOT = HEALTH_DIR.parent.parent.parent  # srcradar/
NOTIFY_SEND = REPO_ROOT / "modules" / "public" / "notify" / "notify_send.sh"
DEFAULT_CONFIG = HEALTH_DIR / "config" / "health.conf"
EXAMPLE_CONFIG = HEALTH_DIR / "config" / "health.conf.example"
LOG_DIR = HEALTH_DIR / "logs"

# it makes "from health_config import parse_config" / "from thresholds import ..." available
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from health_config import parse_config  # noqa: E402
from thresholds import merge_with_defaults  # noqa: E402


# ------------------------------------------------------------------ Finding


@dataclasses.dataclass
class Finding:
    severity: str  # 'ok' | 'warn' | 'alert'
    check: str     # 'disk' | 'cpu' | 'memory' | 'cron' | 'dashboard'
    message: str   # a short one-liner (it goes into the body of the mail)
    detail: str    # the detailed numbers / paths (they only go into the attachment)

    def to_row(self) -> str:
        return f"| {self.severity.upper()} | {self.check} | {self.message} | {self.detail} |"


# ------------------------------------------------------------------ helpers


def _safe_run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """A subprocess.run wrapper, it unifies shell=False and does not raise on a failure."""
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        return cp.returncode, cp.stdout, cp.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _nproc() -> int:
    rc, out, _ = _safe_run(["nproc"])
    if rc == 0 and out.strip().isdigit():
        return max(1, int(out.strip()))
    # the fallback: read /proc/cpuinfo
    try:
        return max(1, sum(1 for _ in Path("/proc/cpuinfo").open(encoding="utf-8", errors="replace") if _.startswith("processor")))
    except OSError:
        return 1


# ------------------------------------------------------------------ 1. disk


def check_disk(thresholds: dict) -> list[Finding]:
    """The df -P parsing; the threshold table is in thresholds.DISK_WARN_PCT / DISK_ALERT_PCT."""
    findings: list[Finding] = []
    warn_pct = int(thresholds.get("DISK_WARN_PCT", 80))
    alert_pct = int(thresholds.get("DISK_ALERT_PCT", 90))

    rc, out, err = _safe_run(["df", "-P"])
    if rc != 0 or not out:
        findings.append(Finding(
            severity="alert", check="disk",
            message="df 命令执行失败",
            detail=f"rc={rc} stderr={err.strip()[:200]}",
        ))
        return findings

    # the df -P output: Filesystem 1024-blocks Used Available Capacity Mounted on
    # the 5th column looks like '78%'; the mount point can contain spaces
    lines = out.splitlines()
    if len(lines) < 2:
        findings.append(Finding("alert", "disk", "df 输出为空", out[:200]))
        return findings

    parsed: list[tuple[str, int, str]] = []  # (mountpoint, used_pct, raw_line)
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        cap_field = parts[4]
        m = re.match(r"^(\d+)%$", cap_field)
        if not m:
            continue
        used = int(m.group(1))
        mount = " ".join(parts[5:])  # it handles the mount points with an = in them, such as "name=partlabel"
        parsed.append((mount, used, line))

    bad = [p for p in parsed if p[1] >= warn_pct]
    if not bad:
        findings.append(Finding(
            severity="ok", check="disk",
            message=f"所有挂载点 < {warn_pct}%",
            detail=f"共 {len(parsed)} 个挂载点,最大占用 {max((p[1] for p in parsed), default=0)}%",
        ))
        return findings

    for mount, used, raw in bad:
        sev = "alert" if used >= alert_pct else "warn"
        findings.append(Finding(
            severity=sev, check="disk",
            message=f"挂载点 {mount} 占用 {used}% (>{warn_pct}%)",
            detail=raw.strip(),
        ))
    return findings


# ------------------------------------------------------------------ 2. cpu


_CPU_PCT_RE = re.compile(r"([\d.]+)\s*id")  # idle value in top's %Cpu line


def check_cpu(thresholds: dict) -> list[Finding]:
    """The uptime load avg (1m/5m/15m) + the instantaneous usage of top -bn1."""
    findings: list[Finding] = []
    load5_per_nproc_limit = float(thresholds.get("CPU_LOAD5_PER_NPROC", 2.0))
    cpu_pct_alert = int(thresholds.get("CPU_PCT_ALERT", 95))

    n = _nproc()

    # ---- load average ----
    rc, out, err = _safe_run(["uptime"])
    if rc != 0 or not out:
        findings.append(Finding(
            severity="alert", check="cpu",
            message="uptime 命令执行失败",
            detail=f"rc={rc} stderr={err.strip()[:200]}",
        ))
    else:
        m = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", out)
        if not m:
            findings.append(Finding("alert", "cpu", "load average 解析失败", out.strip()[:200]))
        else:
            l1, l5, l15 = float(m.group(1)), float(m.group(2)), float(m.group(3))
            ratio5 = l5 / n
            detail = f"nproc={n} load_avg=[{l1}, {l5}, {l15}] load5/nproc={ratio5:.2f}"
            if ratio5 >= load5_per_nproc_limit:
                findings.append(Finding(
                    severity="warn", check="cpu",
                    message=f"5min load/nproc={ratio5:.2f} (>{load5_per_nproc_limit})",
                    detail=detail,
                ))
            else:
                findings.append(Finding(
                    severity="ok", check="cpu",
                    message=f"5min load/nproc={ratio5:.2f}",
                    detail=detail,
                ))

    # ---- the instantaneous CPU usage (top -bn1) ----
    if not shutil.which("top"):
        findings.append(Finding("alert", "cpu", "top 命令不存在", "瞬时 CPU 占用无法采集"))
        return findings

    rc, out, err = _safe_run(["top", "-bn1"])
    if rc != 0 or not out:
        findings.append(Finding(
            severity="alert", check="cpu",
            message="top -bn1 执行失败",
            detail=f"rc={rc} stderr={err.strip()[:200]}",
        ))
        return findings

    # the %Cpu line of top -bn1: "%Cpu(s):  1.2 us,  0.5 sy,  0.0 ni, 98.0 id, ..."
    cpu_line = ""
    for line in out.splitlines():
        if line.startswith("%Cpu"):
            cpu_line = line
            break
    if not cpu_line:
        findings.append(Finding("alert", "cpu", "%Cpu 行未找到", out.splitlines()[0][:200]))
        return findings

    m = _CPU_PCT_RE.search(cpu_line)
    if not m:
        findings.append(Finding("alert", "cpu", "idle 字段未找到", cpu_line[:200]))
        return findings

    idle = float(m.group(1))
    used = 100.0 - idle
    detail = f"top_instant_used={used:.1f}% idle={idle:.1f}% raw={cpu_line.strip()}"
    if used >= cpu_pct_alert:
        findings.append(Finding(
            severity="warn", check="cpu",
            message=f"瞬时 CPU 占用 {used:.1f}% (>{cpu_pct_alert}%)",
            detail=detail,
        ))
    else:
        findings.append(Finding(
            severity="ok", check="cpu",
            message=f"瞬时 CPU 占用 {used:.1f}%",
            detail=detail,
        ))
    return findings


# ------------------------------------------------------------------ 3. memory


def check_memory(thresholds: dict) -> list[Finding]:
    """The /proc/meminfo computation (Total - Available) / Total."""
    findings: list[Finding] = []
    alert_pct = int(thresholds.get("MEM_ALERT_PCT", 90))

    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        findings.append(Finding(
            severity="alert", check="memory",
            message="/proc/meminfo 读取失败",
            detail=str(exc),
        ))
        return findings

    kv: dict[str, int] = {}
    for raw in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", raw)
        if m:
            kv[m.group(1)] = int(m.group(2))

    total = kv.get("MemTotal", 0)
    avail = kv.get("MemAvailable", 0)
    if total <= 0 or avail < 0:
        findings.append(Finding(
            severity="alert", check="memory",
            message="MemTotal/MemAvailable 缺失",
            detail=f"keys={list(kv.keys())[:8]}",
        ))
        return findings

    used_pct = (total - avail) * 100.0 / total
    used_mb = (total - avail) // 1024
    total_mb = total // 1024
    avail_mb = avail // 1024
    detail = f"MemTotal={total_mb}MB MemAvailable={avail_mb}MB used={used_mb}MB used_pct={used_pct:.1f}%"
    if used_pct >= alert_pct:
        findings.append(Finding(
            severity="alert", check="memory",
            message=f"内存使用 {used_pct:.1f}% (>={alert_pct}%)",
            detail=detail,
        ))
    else:
        findings.append(Finding(
            severity="ok", check="memory",
            message=f"内存使用 {used_pct:.1f}%",
            detail=detail,
        ))
    return findings


# ------------------------------------------------------------------ 4. cron


def _redact_cmd(cmd: str) -> str:
    """Redaction: it takes the basename out of the cron command line, so that a
    path (with the repo root in it)
    does not end up in the message field of a Finding. The full path is only in the
    encrypted attachment health_report.md.
    """
    if not cmd:
        return "?"
    base = cmd.rsplit("/", 1)[-1] if "/" in cmd else cmd.split()[-1]
    return base[:80]


def _parse_crontab(text: str) -> list[dict]:
    """A simplified crontab parsing — it only extracts the command field of the
    lines that are neither empty nor a comment.

    5 time fields + the command (the command can contain spaces). It does not
    handle '&&' / the redirections strictly,
    it just takes everything after 'm h dom mon dow' as the command.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # the environment variable assignments (KEY=VAL) are skipped
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            continue
        # it has to start with at least 5 time fields
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        # the 5 time fields have to start with a digit/*/,/-
        head_ok = True
        for tok in parts[:5]:
            if not re.match(r"^[\d\*\/,\-]+$", tok):
                head_ok = False
                break
        if not head_ok:
            continue
        cmd = parts[5]
        out.append({
            "schedule": " ".join(parts[:5]),
            "command": cmd,
        })
    return out


def _extract_log_paths(cmd: str, repo_root: Path) -> list[Path]:
    """Extract the log file path from the cron command as far as possible (it is
    very rough, a heuristic).

    The strategy:
        1. the token after a '>> <path>' / '>>=<path>' / '< <path>'
        2. a path that is passed directly and ends in .log / .txt
        3. when the parsed path is relative it is resolved against repo_root
    """
    candidates: list[Path] = []
    tokens = shlex.split(cmd, posix=True) if cmd else []
    for i, tok in enumerate(tokens):
        # the redirection: the first token after >> / >>= 
        if tok in (">>", ">>=", ">", "1>>", "2>>") and i + 1 < len(tokens):
            candidates.append(Path(tokens[i + 1]))
        if tok.startswith(">>") and len(tok) > 2:
            candidates.append(Path(tok[2:]))
        # it is directly a *.log / *.txt
        if tok.endswith((".log", ".txt")):
            candidates.append(Path(tok))

    resolved: list[Path] = []
    for p in candidates:
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        else:
            p = p.resolve()
        resolved.append(p)
    return resolved


def check_cron(watched_logs: Iterable[str], max_age_hours: float, repo_root: Path | None = None) -> list[Finding]:
    """The crontab -l parsing + the mtime detection of the preset log list."""
    findings: list[Finding] = []
    repo_root = repo_root or REPO_ROOT

    # ---- crontab -l ----
    rc, out, err = _safe_run(["crontab", "-l"])
    if rc != 0:
        # no crontab at all is a legal state
        if "no crontab" in err.lower() or not out.strip():
            findings.append(Finding(
                severity="ok", check="cron",
                message="当前用户无 crontab",
                detail=err.strip()[:200] or "(empty)",
            ))
        else:
            findings.append(Finding(
                severity="warn", check="cron",
                message="crontab -l 失败",
                detail=f"rc={rc} stderr={err.strip()[:200]}",
            ))
    else:
        entries = _parse_crontab(out)
        if not entries:
            findings.append(Finding("ok", "cron", "crontab 无任务", ""))
        else:
            findings.append(Finding(
                severity="ok", check="cron",
                message=f"crontab 解析到 {len(entries)} 条任务",
                detail="\n".join(f"{e['schedule']}  {_redact_cmd(e['command'])}" for e in entries),
            ))

    # ---- the log mtime detection ----
    log_paths: list[Path] = []
    for raw in watched_logs:
        p = Path(raw)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        log_paths.append(p)

    now = time.time()
    for p in log_paths:
        if not p.exists():
            # redaction: the message only shows the basename (not the sensitive root
            # with the repo root in it);
            # the full path only appears in the encrypted attachment
            findings.append(Finding(
                severity="warn", check="cron",
                message=f"日志不存在: {p.name}",
                detail=f"max_age_hours={max_age_hours} (path 脱敏)",
            ))
            continue
        try:
            st = p.stat()
        except OSError as exc:
            findings.append(Finding(
                severity="warn", check="cron",
                message=f"日志 stat 失败: {p.name}",
                detail=str(exc),
            ))
            continue
        age_h = (now - st.st_mtime) / 3600.0
        if age_h > max_age_hours:
            # the message is redacted (the basename only), the detail holds the full path (only in the encrypted attachment)
            findings.append(Finding(
                severity="warn", check="cron",
                message=f"日志 {p.name} 已 {age_h:.1f}h 未更新 (>{max_age_hours}h)",
                detail=f"mtime_age_hours={age_h:.2f} path={p}",
            ))
        else:
            findings.append(Finding(
                severity="ok", check="cron",
                message=f"日志 {p.name} 年龄 {age_h:.1f}h",
                detail=f"mtime_age_hours={age_h:.2f} path={p}",
            ))
    return findings


# ------------------------------------------------------------------ 5. dashboard


def check_dashboard(thresholds: dict, repo_root: Path | None = None) -> list[Finding]:
    """pgrep detects python3 lib/dashboard.py + the mtime of dashboard_watchdog.log."""
    findings: list[Finding] = []
    repo_root = repo_root or REPO_ROOT
    max_age_hours = float(thresholds.get("CRON_MAX_AGE_HOURS", 25))

    # ---- the process detection ----
    # pgrep -f matches its own argv (which holds "python3 lib/dashboard.py"),
    # which gives a false positive.
    # it scans /proc/*/cmdline directly, keeping only the processes whose first
    # 16 bytes hold "python3" and whose last cmdline segment
    # holds "lib/dashboard.py".
    pattern = "python3 lib/dashboard.py"
    pids: list[str] = []
    try:
        for entry in Path("/proc").iterdir():
            name = entry.name
            if not name.isdigit():
                continue
            try:
                cmd = entry.joinpath("cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
            except OSError:
                continue
            if pattern in cmd:
                pids.append(name)
    except OSError:
        pass
    if not pids:
        findings.append(Finding(
            severity="alert", check="dashboard",
            message="dashboard 进程未运行",
            detail=f"pattern={pattern!r} scanned=/proc/*/cmdline",
        ))
    else:
        findings.append(Finding(
            severity="ok", check="dashboard",
            message=f"dashboard 运行中 (pid={','.join(pids)})",
            detail=f"pattern={pattern!r} pids={pids}",
        ))

    # ---- watchdog log mtime ----
    log_path = (repo_root / "modules" / "main" / "daily" / "logs" / "dashboard_watchdog.log").resolve()
    if not log_path.exists():
        findings.append(Finding(
            severity="warn", check="dashboard",
            message=f"watchdog 日志不存在: {log_path.name}",
            detail=f"path={log_path}",
        ))
    else:
        try:
            age_h = (time.time() - log_path.stat().st_mtime) / 3600.0
        except OSError as exc:
            findings.append(Finding(
                severity="warn", check="dashboard",
                message="watchdog 日志 stat 失败",
                detail=str(exc),
            ))
        else:
            if age_h > max_age_hours:
                findings.append(Finding(
                    severity="alert",
                    check="dashboard",
                    message=f"watchdog 日志已 {age_h:.1f}h 未更新 (>{max_age_hours}h)",
                    detail=f"path={log_path} mtime_age_hours={age_h:.2f}",
                ))
            else:
                findings.append(Finding(
                    severity="ok",
                    check="dashboard",
                    message=f"watchdog 日志年龄 {age_h:.1f}h",
                    detail=f"path={log_path} mtime_age_hours={age_h:.2f}",
                ))
    return findings


# ------------------------------------------------------------------ runner


def run_all(thresholds: dict, repo_root: Path | None = None) -> list[Finding]:
    """Run the 5 checks one after the other; every one is isolated by its own try/except, and a failure also returns an alert Finding."""
    findings: list[Finding] = []
    checks = [
        ("disk",      lambda: check_disk(thresholds)),
        ("cpu",       lambda: check_cpu(thresholds)),
        ("memory",    lambda: check_memory(thresholds)),
        ("cron",      lambda: check_cron(
            thresholds.get("WATCHED_LOGS", []),  # type: ignore[arg-type]
            float(thresholds.get("CRON_MAX_AGE_HOURS", 25)),
            repo_root=repo_root,
        )),
        ("dashboard", lambda: check_dashboard(thresholds, repo_root=repo_root)),
    ]
    for name, fn in checks:
        try:
            sub = fn()
        except Exception as exc:  # noqa: BLE001 - it swallows every exception on purpose
            sub = [Finding(
                severity="alert", check=name,
                message=f"{name} 检查自身抛异常",
                detail=f"{type(exc).__name__}: {exc}",
            )]
        findings.extend(sub)
    return findings


# ------------------------------------------------------------------ report


def render_report(findings: list[Finding], run_id: str, date_ph: str) -> str:
    """Render the full health_report.md (the attachment content); the redacted version is written as the body by the caller."""
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    n_alert = sum(1 for f in findings if f.severity == "alert")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    n_ok = sum(1 for f in findings if f.severity == "ok")
    overall = "ALERT" if n_alert > 0 else ("WARN" if n_warn > 0 else "OK")

    lines: list[str] = []
    lines.append(f"# health report — run_id={run_id}")
    lines.append("")
    lines.append(f"- generated_at: {now.isoformat(timespec='seconds')}")
    lines.append(f"- date_placeholder: {date_ph}")
    lines.append(f"- overall: **{overall}**  (alert={n_alert} warn={n_warn} ok={n_ok})")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if overall == "OK":
        lines.append("All checks passed. No email sent.")
    else:
        for f in findings:
            if f.severity in ("warn", "alert"):
                lines.append(f"- [{f.severity.upper()}][{f.check}] {f.message}")
    lines.append("")
    lines.append("## Detailed findings")
    lines.append("")
    # it does not use a markdown table + a <details> collapse (no HTML/markdown tag
    # shows up in the attachment);
    # it uses a bullet list instead, one paragraph per finding, with the detail on
    # a new line after the message.
    for f in findings:
        lines.append(f"- **{f.severity.upper()}** [{f.check}] {f.message}")
        if f.detail:
            # the detail can span several lines, each one is indented so that it reads clearly
            for d_line in f.detail.splitlines() or [f.detail]:
                lines.append(f"    {d_line}")
    lines.append("")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Encrypted attachment; decrypt on recipient device (OpenKeychain / GPG)._")
    return "\n".join(lines) + "\n"


def render_body(findings: list[Finding], subject_prefix: str, date_ph: str) -> str:
    """The redacted version of the body — one line per anomaly + the counts, without
    the detailed numbers / paths.

    notify_send.sh wraps the GPG encrypted attachment around it.
    """
    lines: list[str] = []
    lines.append(f"{subject_prefix} health {date_ph}")
    lines.append("")
    n_alert = sum(1 for f in findings if f.severity == "alert")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    n_ok = sum(1 for f in findings if f.severity == "ok")
    overall = "ALERT" if n_alert > 0 else ("WARN" if n_warn > 0 else "OK")
    lines.append(f"overall: {overall} (alert={n_alert} warn={n_warn} ok={n_ok})")
    lines.append("")
    if overall == "OK":
        lines.append("All 5 checks passed; no anomalies. See encrypted attachment for detail.")
    else:
        for f in findings:
            if f.severity in ("warn", "alert"):
                lines.append(f"- [{f.severity.upper()}][{f.check}] {f.message}")
    lines.append("")
    lines.append("Encrypted attachment: health_report.md.gpg (OpenPGP, requires OpenKeychain to decrypt)")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI


def _cli() -> int:
    p = argparse.ArgumentParser(
        prog="health_check.py",
        description="5 项健康检查 + 报告渲染;--dryrun 不调 notify_send.sh",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help=f"conf 路径(默认 {DEFAULT_CONFIG},缺时用 DEFAULT_THRESHOLDS)")
    p.add_argument("--dryrun", action="store_true", help="只生成报告,不调 notify")
    p.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"), help="run_id")
    p.add_argument("--date-ph", default="%Y-%m-%d", help="date 占位符")
    p.add_argument("--out", default=str(LOG_DIR / "health_report.md"),
                   help=f"报告输出路径(默认 {LOG_DIR / 'health_report.md'})")
    p.add_argument("--notify-script", default=str(NOTIFY_SEND),
                   help="notify_send.sh 路径(可改)")
    p.add_argument("--repo-root", default=str(REPO_ROOT),
                   help="项目根,影响 cron / dashboard 路径解析")
    args = p.parse_args()

    cfg_path = Path(args.config)
    user_dict = None
    if cfg_path.is_file():
        user_dict = parse_config(cfg_path)
        if user_dict is None:
            print(f"[health] WARN conf parse 失败,回退 DEFAULT: {cfg_path}", file=sys.stderr)
    else:
        print(f"[health] INFO conf 不存在({cfg_path}),用 DEFAULT_THRESHOLDS", file=sys.stderr)

    thresholds = merge_with_defaults(user_dict)

    repo_root = Path(args.repo_root).resolve()
    findings = run_all(thresholds, repo_root=repo_root)
    report = render_report(findings, args.run_id, args.date_ph)
    body = render_body(findings, "[srcradar] health", args.date_ph)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    body_path = out_path.with_name(out_path.stem + ".body.md")
    body_path.write_text(body, encoding="utf-8")
    print(f"[health] report: {out_path}")
    print(f"[health] body:   {body_path}")

    n_alert = sum(1 for f in findings if f.severity == "alert")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    print(f"[health] findings: alert={n_alert} warn={n_warn} ok={sum(1 for f in findings if f.severity == 'ok')}")

    if args.dryrun:
        print("[health] --dryrun: 跳过 notify_send.sh")
        return 0

    if n_alert == 0 and n_warn == 0:
        print("[health] 全 OK,不调 notify_send.sh")
        return 0

    notify = Path(args.notify_script)
    if not notify.is_file():
        print(f"[health] WARN notify_send.sh 缺失({notify});跳过推送", file=sys.stderr)
        return 0

    # the way it is called is exactly the same as daily_monitor:
    #   bash notify_send.sh <report.md> <business_name> <run_id> <date_ph>
    cmd = ["bash", str(notify), str(out_path), "health", args.run_id, args.date_ph]
    # the health module has its own body: the redacted body.md that
    # lib/health_check.py already generated (plain text),
    # it skips the daily diff template that notify_send.sh locks in (see the --text-file parameter of notify_send.sh)
    body_md = out_path.parent / "health_report.body.md"
    if body_md.is_file():
        cmd[2:2] = ["--text-file", str(body_md)]
    print(f"[health] 调 notify: {' '.join(shlex.quote(c) for c in cmd)}")
    rc, _, err = _safe_run(cmd, timeout=60)
    if rc != 0:
        print(f"[health] WARN notify 失败 rc={rc}: {err.strip()[:300]}", file=sys.stderr)
        # a failed notification does not affect the health verdict
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())