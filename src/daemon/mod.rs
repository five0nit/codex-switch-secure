pub mod loop_runner;
pub mod notify;
pub mod pidfile;
pub mod service;

use crate::cli::DaemonCommand;
use crate::output::user_println;
use anyhow::Result;

pub async fn dispatch(cmd: DaemonCommand) -> Result<()> {
    match cmd {
        DaemonCommand::Start { foreground } => start(foreground).await,
        DaemonCommand::Stop => stop(),
        DaemonCommand::Status => status(),
        DaemonCommand::Install => service::install(),
        DaemonCommand::Uninstall => service::uninstall(),
    }
}

async fn start(foreground: bool) -> Result<()> {
    #[cfg(not(unix))]
    {
        let _ = foreground;
        anyhow::bail!(
            "The background daemon is only supported on Unix (macOS/Linux). \
             On Windows, run codex-switch commands directly or use Task Scheduler."
        );
    }
    #[cfg(unix)]
    {
        if pidfile::is_daemon_running() {
            anyhow::bail!(
                "Daemon is already running (PID {})",
                pidfile::read_pidfile().unwrap_or(0)
            );
        }
        // Clean up stale PID file before starting
        pidfile::cleanup_pidfile()?;
        if foreground {
            run_foreground().await
        } else {
            start_detached()
        }
    }
}

#[cfg(unix)]
async fn run_foreground() -> Result<()> {
    pidfile::write_pidfile_exclusive()?;
    // RAII guard ensures PID file is cleaned up even on panic
    let _guard = pidfile::PidGuard;
    tracing::info!("codex-switch daemon started (PID {})", std::process::id());
    loop_runner::run_daemon_loop().await
}

#[cfg(unix)]
fn start_detached() -> Result<()> {
    let exe = std::env::current_exe()?;
    let mut child = std::process::Command::new(exe)
        .args(["daemon", "start", "--foreground"])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()?;

    let pid = child.id();
    // Wait for the daemon to write its PID file, which signals it reached the
    // event loop. Polling the actual readiness signal is more reliable than a
    // fixed sleep on slow disks / CI / containers.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        // Did the child exit before initializing?
        if let Ok(Some(status)) = child.try_wait() {
            anyhow::bail!(
                "Daemon process (PID {pid}) exited immediately ({status}); check logs for details"
            );
        }
        if pidfile::read_pidfile() == Some(pid) {
            user_println(&format!("Daemon started (PID {pid})"));
            return Ok(());
        }
        if std::time::Instant::now() >= deadline {
            anyhow::bail!(
                "Daemon (PID {pid}) did not initialize within 2s (no PID file written); check logs"
            );
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

fn stop() -> Result<()> {
    let pid = pidfile::read_pidfile()
        .ok_or_else(|| anyhow::anyhow!("No daemon PID file found; daemon may not be running"))?;
    if !pidfile::process_alive(pid) {
        pidfile::cleanup_pidfile()?;
        user_println("Daemon was not running (stale PID file cleaned up)");
        return Ok(());
    }
    pidfile::send_sigterm(pid)?;
    user_println(&format!("Sent stop signal to daemon (PID {pid})"));
    Ok(())
}

fn status() -> Result<()> {
    match pidfile::read_pidfile() {
        Some(pid) if pidfile::process_alive(pid) => {
            user_println(&format!("Daemon is running (PID {pid})"));
        }
        Some(pid) => {
            user_println(&format!("Daemon is not running (stale PID {pid})"));
            pidfile::cleanup_pidfile()?;
        }
        None => {
            user_println("Daemon is not running");
        }
    }
    Ok(())
}
