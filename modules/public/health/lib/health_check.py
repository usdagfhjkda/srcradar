"""
modules/public/health/lib/health_check.py — 5 项健康检查 + 报告渲染

检查项:
    1. disk       df -P 解析,挑 ≥阈值 的挂载点
    2. cpu        uptime load avg (1m/5m/15m) + top -bn1 瞬时占用
    3. memory     /proc/meminfo 计算 (Total - Available) / Total
    4. cron       crontab -l 解析,看每个任务日志 mtime
    5. dashboard  pgrep 检测 python3 lib/dashboard.py

每个 check 函数独立 try/except,失败也返回 Finding(severity=alert)
而不抛出 → 单项异常不拖垮其他检查。

用法:
    python3 health_check.py [--config <health.conf>] [--dryrun] [--run-id <id>] [--date-ph <ph>] [--out <path>]
    --config  用指定 conf(默认 config/health.conf.example 的 DEFAULT 即可跑通)
    --dryrun  只生成 health_report.md,不调 notify_send.sh
    --run-id  自定义 run_id (默认时间戳)
    --date-ph 自定义 date 占位符 (默认 %Y-%m-%d)
    --out     自定义报告输出路径 (默认 logs/health_report.md)

退出码:0=ok 1=参数 3=某 check 自身抛异常(已写 alert Finding)
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

# 本模块路径相关
SCRIPT_DIR = Path(__file__).resolve().parent
HEALTH_DIR = SCRIPT_DIR.parent  # modules/public/health
REPO_ROOT = HEALTH_DIR.parent.parent.parent  # srcradar/
NOTIFY_SEND = REPO_ROOT / "modules" / "public" / "notify" / "notify_send.sh"
DEFAULT_CONFIG = HEALTH_DIR / "config" / "health.conf"
EXAMPLE_CONFIG = HEALTH_DIR / "config" / "health.conf.example"
LOG_DIR = HEALTH_DIR / "logs"

# 让 "from health_config import parse_config" / "from thresholds import ..." 可用
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from health_config import parse_config  # noqa: E402
from thresholds import merge_with_defaults  # noqa: E402


# ------------------------------------------------------------------ Finding


@dataclasses.dataclass
class Finding:
    severity: str  # 'ok' | 'warn' | 'alert'
    check: str     # 'disk' | 'cpu' | 'memory' | 'cron' | 'dashboard'
    message: str   # 简短一句话(进入邮件正文)
    detail: str    # 详细数值 / 路径(只进附件)

    def to_row(self) -> str:
        return f"| {self.severity.upper()} | {self.check} | {self.message} | {self.detail} |"


# ------------------------------------------------------------------ helpers


def _safe_run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    """subprocess.run 包裹,统一 shell=False,失败不抛。"""
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
    # 兜底:读 /proc/cpuinfo
    try:
        return max(1, sum(1 for _ in Path("/proc/cpuinfo").open(encoding="utf-8", errors="replace") if _.startswith("processor")))
    except OSError:
        return 1


# ------------------------------------------------------------------ 1. disk


def check_disk(thresholds: dict) -> list[Finding]:
    """df -P 解析;阈值表见 thresholds.DISK_WARN_PCT / DISK_ALERT_PCT。"""
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

    # df -P 输出:Filesystem 1024-blocks Used Available Capacity Mounted on
    # 第 5 列是 '78%' 这种;挂载点可能含空格
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
        mount = " ".join(parts[5:])  # 处理 'name=partlabel' 这类带 = 的挂载点
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
    """uptime load avg (1m/5m/15m) + top -bn1 瞬时占用。"""
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

    # ---- 瞬时 CPU 占用(top -bn1)----
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

    # top -bn1 的 %Cpu 行: "%Cpu(s):  1.2 us,  0.5 sy,  0.0 ni, 98.0 id, ..."
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
    """/proc/meminfo 计算 (Total - Available) / Total。"""
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
    """脱敏:从 cron 命令行里提取 basename,避免路径(含 the repo root's  等)
    出现在 Finding 的 message 字段。完整路径只在加密附件 health_report.md 里。
    """
    if not cmd:
        return "?"
    base = cmd.rsplit("/", 1)[-1] if "/" in cmd else cmd.split()[-1]
    return base[:80]


def _parse_crontab(text: str) -> list[dict]:
    """简化版 crontab 解析 — 仅提取非空非注释行的 command 字段。

    5 个时间字段 + 命令(命令可能含空格)。不严格处理 '&&' / 重定向,
    只把 'm h dom mon dow' 后面的整段当作命令。
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 跳过环境变量赋值 (KEY=VAL)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
            continue
        # 至少 5 个时间字段开头
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        # 5 个时间字段开头必须是数字/*/,/-
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
    """从 cron 命令里尽量提日志文件路径(很粗略,启发式)。

    策略:
        1. 形如 '>> <path>' '>>=<path>' '< <path>' 后跟的 token
        2. 直接传 .log / .txt 路径
        3. 解析后若有相对路径,以 repo_root 解析
    """
    candidates: list[Path] = []
    tokens = shlex.split(cmd, posix=True) if cmd else []
    for i, tok in enumerate(tokens):
        # 重定向: >> / >>= 后第一个 token
        if tok in (">>", ">>=", ">", "1>>", "2>>") and i + 1 < len(tokens):
            candidates.append(Path(tokens[i + 1]))
        if tok.startswith(">>") and len(tok) > 2:
            candidates.append(Path(tok[2:]))
        # 直接是 *.log / *.txt
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
    """crontab -l 解析 + 预置日志列表 mtime 检测。"""
    findings: list[Finding] = []
    repo_root = repo_root or REPO_ROOT

    # ---- crontab -l ----
    rc, out, err = _safe_run(["crontab", "-l"])
    if rc != 0:
        # 没有 crontab 是合法状态
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

    # ---- 日志 mtime 检测 ----
    log_paths: list[Path] = []
    for raw in watched_logs:
        p = Path(raw)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        log_paths.append(p)

    now = time.time()
    for p in log_paths:
        if not p.exists():
            # 脱敏:message 只显示 basename(不含 the repo root's  等敏感根);
            # 完整 path 仅出现在加密附件里
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
            # message 已脱敏(只用 basename),detail 含完整 path(仅在加密附件里)
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
    """pgrep 检测 python3 lib/dashboard.py + dashboard_watchdog.log mtime。"""
    findings: list[Finding] = []
    repo_root = repo_root or REPO_ROOT
    max_age_hours = float(thresholds.get("CRON_MAX_AGE_HOURS", 25))

    # ---- 进程检测 ----
    # pgrep -f 会匹配自己的 argv(含 "python3 lib/dashboard.py"),导致假阳性。
    # 直接扫 /proc/*/cmdline,只保留前 16 字节含 "python3" 且整 cmdline 末段
    # 含 "lib/dashboard.py" 的进程。
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
    """依次跑 5 个 check;每个 try/except 隔离,失败也返回 alert Finding。"""
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
        except Exception as exc:  # noqa: BLE001 - 故意吃掉所有异常
            sub = [Finding(
                severity="alert", check=name,
                message=f"{name} 检查自身抛异常",
                detail=f"{type(exc).__name__}: {exc}",
            )]
        findings.extend(sub)
    return findings


# ------------------------------------------------------------------ report


def render_report(findings: list[Finding], run_id: str, date_ph: str) -> str:
    """渲染完整 health_report.md(附件内容);脱敏版另由 caller 写 body。"""
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
    # 不用 markdown 表格 + <details> 折叠(附件里不出现 HTML/markdown 标签);
    # 改用 bullet list,每项 finding 一段,detail 跟在 message 后面换行。
    for f in findings:
        lines.append(f"- **{f.severity.upper()}** [{f.check}] {f.message}")
        if f.detail:
            # detail 内容可能多行,每行前加缩进使查看明确
            for d_line in f.detail.splitlines() or [f.detail]:
                lines.append(f"    {d_line}")
    lines.append("")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Encrypted attachment; decrypt on recipient device (OpenKeychain / GPG)._")
    return "\n".join(lines) + "\n"


def render_body(findings: list[Finding], subject_prefix: str, date_ph: str) -> str:
    """正文脱敏版 — 每异常一行 + 计数,不含详细数值/路径。

    notify_send.sh 会在外面再 wrap GPG 加密附件。
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

    # 调用方式与 daily_monitor 完全一致:
    #   bash notify_send.sh <report.md> <business_name> <run_id> <date_ph>
    cmd = ["bash", str(notify), str(out_path), "health", args.run_id, args.date_ph]
    # health 模块自定义正文:用 lib/health_check.py 已生成的脱敏版 body.md(plain text),
    # 跳过 notify_send.sh 锁死的 daily diff 模板(详见 notify_send.sh --text-file 参数)
    body_md = out_path.parent / "health_report.body.md"
    if body_md.is_file():
        cmd[2:2] = ["--text-file", str(body_md)]
    print(f"[health] 调 notify: {' '.join(shlex.quote(c) for c in cmd)}")
    rc, _, err = _safe_run(cmd, timeout=60)
    if rc != 0:
        print(f"[health] WARN notify 失败 rc={rc}: {err.strip()[:300]}", file=sys.stderr)
        # 通知失败不影响健康结论
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())