import typer

cron_app = typer.Typer(name="cron", help="Manage cron scheduler and jobs")

@cron_app.command("start")
def cron_start() -> None:
    """Start the cron scheduler daemon."""
    from openharness.services.cron_scheduler import is_scheduler_running, start_daemon

    if is_scheduler_running():
        print("Cron scheduler is already running.")
        return
    pid = start_daemon()
    print(f"Cron scheduler started (pid={pid})")


@cron_app.command("stop")
def cron_stop() -> None:
    """Stop the cron scheduler daemon."""
    from openharness.services.cron_scheduler import stop_scheduler

    if stop_scheduler():
        print("Cron scheduler stopped.")
    else:
        print("Cron scheduler is not running.")


@cron_app.command("status")
def cron_status_cmd() -> None:
    """Show cron scheduler status and job summary."""
    from openharness.services.cron_scheduler import scheduler_status

    status = scheduler_status()
    state = "running" if status["running"] else "stopped"
    print(f"Scheduler: {state}" + (f" (pid={status['pid']})" if status["pid"] else ""))
    print(f"Jobs:      {status['enabled_jobs']} enabled / {status['total_jobs']} total")
    print(f"Log:       {status['log_file']}")


@cron_app.command("list")
def cron_list_cmd() -> None:
    """List all registered cron jobs with schedule and status."""
    from openharness.services.cron import load_cron_jobs

    jobs = load_cron_jobs()
    if not jobs:
        print("No cron jobs configured.")
        return
    for job in jobs:
        enabled = "on " if job.get("enabled", True) else "off"
        last = job.get("last_run", "never")
        if last != "never":
            last = last[:19]  # trim to readable datetime
        last_status = job.get("last_status", "")
        status_indicator = f" [{last_status}]" if last_status else ""
        print(f"  [{enabled}] {job['name']}  {job.get('schedule', '?')}")
        print(f"        cmd: {job['command']}")
        print(f"        last: {last}{status_indicator}  next: {job.get('next_run', 'n/a')[:19]}")


@cron_app.command("toggle")
def cron_toggle_cmd(
    name: str = typer.Argument(..., help="Cron job name"),
    enabled: bool = typer.Argument(..., help="true to enable, false to disable"),
) -> None:
    """Enable or disable a cron job."""
    from openharness.services.cron import set_job_enabled

    if not set_job_enabled(name, enabled):
        print(f"Cron job not found: {name}")
        raise typer.Exit(1)
    state = "enabled" if enabled else "disabled"
    print(f"Cron job '{name}' is now {state}")


@cron_app.command("history")
def cron_history_cmd(
    name: str | None = typer.Argument(None, help="Filter by job name"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries"),
) -> None:
    """Show cron execution history."""
    from openharness.services.cron_scheduler import load_history

    entries = load_history(limit=limit, job_name=name)
    if not entries:
        print("No execution history.")
        return
    for entry in entries:
        ts = entry.get("started_at", "?")[:19]
        status = entry.get("status", "?")
        rc = entry.get("returncode", "?")
        print(f"  {ts}  {entry.get('name', '?')}  {status} (rc={rc})")
        stderr = entry.get("stderr", "").strip()
        if stderr and status != "success":
            for line in stderr.splitlines()[:3]:
                print(f"    stderr: {line}")


@cron_app.command("logs")
def cron_logs_cmd(
    lines: int = typer.Option(30, "--lines", "-n", help="Number of lines to show"),
) -> None:
    """Show recent cron scheduler log output."""
    from openharness.config.paths import get_logs_dir

    log_path = get_logs_dir() / "cron_scheduler.log"
    if not log_path.exists():
        print("No scheduler log found. Start the scheduler with: oh cron start")
        return
    content = log_path.read_text(encoding="utf-8", errors="replace")
    tail = content.splitlines()[-lines:]
    for line in tail:
        print(line)