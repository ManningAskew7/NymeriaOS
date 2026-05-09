//! Child process lifecycle management for source-checkout Nymeria backend,
//! worker, and CLIProxy.
//!
//! Uses Windows Job Objects to guarantee child processes die when the parent exits,
//! even on crash. All processes are spawned without console windows.

use shared_child::SharedChild;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const CLIPROXY_HOST_BASE_URL: &str = "http://127.0.0.1:8318";
const CLIPROXY_READY_TEXT: &str = "CLI Proxy API Server";

#[derive(Clone, Debug)]
pub struct RuntimeLayout {
    project_root: PathBuf,
    backend_root: PathBuf,
}

impl RuntimeLayout {
    fn source_checkout(project_root: PathBuf) -> Self {
        let backend_root = project_root.join("Nymeria");
        Self {
            project_root,
            backend_root,
        }
    }

    pub fn backend_root(&self) -> &PathBuf {
        &self.backend_root
    }
}

/// Wrapper for the Windows Job Object handle (auto-kills children on drop).
#[cfg(windows)]
struct JobObject {
    handle: HANDLE,
}

#[cfg(windows)]
impl JobObject {
    fn new() -> Option<Self> {
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return None;
            }

            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation = JOBOBJECT_BASIC_LIMIT_INFORMATION {
                LimitFlags: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                ..std::mem::zeroed()
            };

            let result = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );

            if result == 0 {
                CloseHandle(handle);
                return None;
            }

            Some(Self { handle })
        }
    }

    fn assign_child(&self, child: &std::process::Child) {
        unsafe {
            AssignProcessToJobObject(self.handle, child.as_raw_handle() as HANDLE);
        }
    }
}

#[cfg(windows)]
impl Drop for JobObject {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}

// Send + Sync are safe because the Job Object handle is only used behind a mutex
#[cfg(windows)]
unsafe impl Send for JobObject {}
#[cfg(windows)]
unsafe impl Sync for JobObject {}

pub struct ProcessManager {
    project_root: PathBuf,
    backend_root: PathBuf,
    api_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    worker_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    cliproxy_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    #[cfg(windows)]
    job_object: Option<JobObject>,
}

impl ProcessManager {
    pub fn new(layout: RuntimeLayout) -> Self {
        Self {
            project_root: layout.project_root,
            backend_root: layout.backend_root,
            api_process: Arc::new(Mutex::new(None)),
            worker_process: Arc::new(Mutex::new(None)),
            cliproxy_process: Arc::new(Mutex::new(None)),
            #[cfg(windows)]
            job_object: JobObject::new(),
        }
    }

    /// Walk up from the executable's location to find a source checkout.
    /// Installed builds intentionally return an error so the app runs as a
    /// client-only frontend.
    pub fn detect_runtime_layout() -> Result<RuntimeLayout, String> {
        // Check env var override first
        if let Ok(root) = std::env::var("NYMERIA_PROJECT_ROOT") {
            let path = PathBuf::from(&root);
            // NYMERIA_PROJECT_ROOT might point to the Nymeria/ subdir or the repo root
            if path.join("nymeria").exists() && path.join("run.py").exists() {
                // Points to Nymeria/ subdir — go up one level
                if let Some(parent) = path.parent() {
                    return Ok(RuntimeLayout::source_checkout(parent.to_path_buf()));
                }
            }
            if is_source_checkout_root(&path) {
                return Ok(RuntimeLayout::source_checkout(path));
            }
        }

        let exe = std::env::current_exe()
            .map_err(|e| format!("Cannot determine exe path: {}", e))?;

        let mut dir = exe
            .parent()
            .ok_or("Cannot get exe parent dir")?
            .to_path_buf();

        // Walk up to 10 levels to find the project root
        for _ in 0..10 {
            if is_source_checkout_root(&dir) {
                return Ok(RuntimeLayout::source_checkout(dir));
            }
            if !dir.pop() {
                break;
            }
        }

        Err(
            "Could not find a NymeriaOS source checkout. \
             Installed builds run in client-only mode; set NYMERIA_PROJECT_ROOT \
             to a checkout root for development backend management."
                .to_string(),
        )
    }

    /// Spawn the backend API server from a source checkout (`python run.py api`).
    pub fn start_api(&self) -> Result<(), String> {
        let mut command = self.backend_command("api")?;

        let child = spawn_no_window(&mut command)
            .map_err(|e| format!("Failed to spawn backend API: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.api_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Spawn the worker (ticker daemon).
    pub fn start_worker(&self) -> Result<(), String> {
        let mut command = self.backend_command("worker")?;

        let child = spawn_no_window(&mut command)
            .map_err(|e| format!("Failed to spawn worker: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.worker_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Start the current pinned CLIProxy Docker compose deployment.
    pub fn start_cliproxy(&self) -> Result<(), String> {
        if self.is_cliproxy_running() {
            return Ok(());
        }

        let cliproxy_root = self.cliproxy_root();
        let compose_path = self.cliproxy_compose_path()?;
        let mut command = Command::new("docker");
        command
            .arg("compose")
            .arg("-f")
            .arg(&compose_path)
            .arg("up")
            .arg("-d")
            .current_dir(&cliproxy_root);

        let output = command
            .output()
            .map_err(|e| format!("Failed to run docker compose up -d: {}", e))?;
        if !output.status.success() {
            return Err(command_failure("docker compose up -d", output));
        }

        if !Self::wait_for_cliproxy_ready(Duration::from_secs(20)) {
            return Err(format!(
                "CLIProxy did not become reachable at {} after docker compose up.",
                CLIPROXY_HOST_BASE_URL
            ));
        }
        Ok(())
    }

    /// Stop the current pinned CLIProxy Docker compose deployment.
    pub fn stop_cliproxy(&self) -> Result<(), String> {
        let cliproxy_root = self.cliproxy_root();
        let compose_path = self.cliproxy_compose_path()?;
        let mut command = Command::new("docker");
        command
            .arg("compose")
            .arg("-f")
            .arg(&compose_path)
            .arg("stop")
            .current_dir(&cliproxy_root);

        let output = command
            .output()
            .map_err(|e| format!("Failed to run docker compose stop: {}", e))?;
        if !output.status.success() {
            return Err(command_failure("docker compose stop", output));
        }
        Ok(())
    }

    /// Check if the host-reachable CLIProxy endpoint is alive.
    pub fn is_cliproxy_running(&self) -> bool {
        Self::cliproxy_root_available()
    }

    /// Check if the API process is alive.
    pub fn is_api_running(&self) -> bool {
        Self::is_process_alive(&self.api_process)
    }

    /// Check if the worker process is alive.
    pub fn is_worker_running(&self) -> bool {
        Self::is_process_alive(&self.worker_process)
    }

    /// Poll the health endpoint until the API is ready or timeout.
    pub fn wait_for_api_ready(&self, timeout_secs: u64) -> Result<(), String> {
        let deadline = Instant::now() + Duration::from_secs(timeout_secs);
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .map_err(|e| format!("HTTP client error: {}", e))?;

        loop {
            if Instant::now() > deadline {
                return Err("Backend API did not become ready within timeout".to_string());
            }

            // Check if the API process is still alive
            if !Self::is_process_alive(&self.api_process) {
                return Err("Backend API process exited unexpectedly".to_string());
            }

            match client.get("http://127.0.0.1:8000/health").send() {
                Ok(resp) if resp.status().is_success() => return Ok(()),
                _ => std::thread::sleep(Duration::from_millis(500)),
            }
        }
    }

    /// Gracefully shut down all managed processes.
    pub fn shutdown_all(&self) {
        Self::kill_process(&self.cliproxy_process);
        Self::kill_process(&self.worker_process);
        Self::kill_process(&self.api_process);

        // Give processes time to exit
        std::thread::sleep(Duration::from_secs(1));
    }

    /// Get the project root path.
    pub fn project_root(&self) -> &PathBuf {
        &self.project_root
    }

    // --- Private helpers ---

    /// Assign a child process to the job object and wrap in SharedChild.
    fn wrap_child(&self, child: std::process::Child) -> Result<Arc<SharedChild>, String> {
        #[cfg(windows)]
        if let Some(ref job) = self.job_object {
            job.assign_child(&child);
        }

        let shared = SharedChild::new(child)
            .map_err(|e| format!("Failed to wrap child process: {}", e))?;
        Ok(Arc::new(shared))
    }

    fn backend_command(&self, service: &str) -> Result<Command, String> {
        let run_py = self.backend_root.join("run.py");
        if !run_py.is_file() {
            return Err(format!(
                "Backend source entry point not found at {}",
                run_py.display()
            ));
        }

        let mut command = Command::new(python_executable());
        command
            .arg("run.py")
            .arg(service)
            .current_dir(&self.backend_root)
            .env("NYMERIA_PROJECT_ROOT", &self.backend_root);
        Ok(command)
    }

    fn cliproxy_root(&self) -> PathBuf {
        self.project_root
            .join("CLIProxyAPI-main")
            .join("temp")
            .join("latest")
    }

    fn cliproxy_compose_path(&self) -> Result<PathBuf, String> {
        let compose_path = self.cliproxy_root().join("docker-compose.yml");
        if !compose_path.is_file() {
            return Err(format!(
                "Pinned CLIProxy compose file not found at {}",
                compose_path.display()
            ));
        }
        Ok(compose_path)
    }

    fn wait_for_cliproxy_ready(timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() <= deadline {
            if Self::cliproxy_root_available() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        false
    }

    fn cliproxy_root_available() -> bool {
        let client = match reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
        {
            Ok(client) => client,
            Err(_) => return false,
        };

        match client.get(format!("{}/", CLIPROXY_HOST_BASE_URL)).send() {
            Ok(resp) if resp.status().as_u16() < 500 => match resp.text() {
                Ok(body) => body.contains(CLIPROXY_READY_TEXT),
                Err(_) => false,
            },
            _ => false,
        }
    }

    fn is_process_alive(proc: &Arc<Mutex<Option<Arc<SharedChild>>>>) -> bool {
        let guard = proc.lock().unwrap();
        match guard.as_ref() {
            Some(child) => child.try_wait().ok().flatten().is_none(),
            None => false,
        }
    }

    fn kill_process(proc: &Arc<Mutex<Option<Arc<SharedChild>>>>) {
        let mut guard = proc.lock().unwrap();
        if let Some(child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn is_source_checkout_root(path: &Path) -> bool {
    path.join("Nymeria").join("run.py").exists()
        && path.join("nymeria-desktop").is_dir()
}

fn python_executable() -> String {
    std::env::var("NYMERIA_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn spawn_no_window(command: &mut Command) -> std::io::Result<Child> {
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    command.spawn()
}

fn command_failure(label: &str, output: Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let detail = if !stderr.trim().is_empty() {
        stderr.trim()
    } else {
        stdout.trim()
    };

    if detail.is_empty() {
        format!("{} failed with status {}", label, output.status)
    } else {
        format!("{} failed with status {}: {}", label, output.status, detail)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::ffi::OsStr;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct TempTree {
        path: PathBuf,
    }

    impl TempTree {
        fn new() -> Self {
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock before unix epoch")
                .as_nanos();
            let path = std::env::temp_dir().join(format!(
                "nymeria-process-manager-test-{}-{}",
                std::process::id(),
                nanos
            ));
            fs::create_dir_all(&path).expect("create temp tree");
            Self { path }
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    fn touch(path: &Path) {
        fs::create_dir_all(path.parent().expect("path has parent")).expect("create parent");
        fs::write(path, b"").expect("write file");
    }

    #[test]
    fn recognizes_source_checkout_root() {
        let temp = TempTree::new();
        touch(&temp.path.join("Nymeria").join("run.py"));
        fs::create_dir_all(temp.path.join("nymeria-desktop")).expect("create desktop dir");

        assert!(is_source_checkout_root(&temp.path));
    }

    #[test]
    fn ignores_dist_output_without_source_checkout() {
        let temp = TempTree::new();
        touch(
            &temp
                .path
                .join("Nymeria")
                .join("dist")
                .join("backend-placeholder.exe"),
        );

        assert!(!is_source_checkout_root(&temp.path));
    }

    #[test]
    fn source_backend_command_runs_run_py_from_backend_root() {
        let temp = TempTree::new();
        touch(&temp.path.join("Nymeria").join("run.py"));

        let layout = RuntimeLayout::source_checkout(temp.path.clone());
        let manager = ProcessManager::new(layout);
        let command = manager.backend_command("api").expect("backend command");
        let args: Vec<_> = command.get_args().collect();
        let backend_root = temp.path.join("Nymeria");

        assert_eq!(args, vec![OsStr::new("run.py"), OsStr::new("api")]);
        assert_eq!(command.get_current_dir(), Some(backend_root.as_path()));
        assert_eq!(
            command
                .get_envs()
                .find(|(key, _)| key == OsStr::new("NYMERIA_PROJECT_ROOT"))
                .and_then(|(_, value)| value),
            Some(backend_root.as_os_str())
        );
    }

    #[test]
    fn backend_command_requires_run_py() {
        let temp = TempTree::new();
        fs::create_dir_all(temp.path.join("Nymeria")).expect("create backend dir");

        let layout = RuntimeLayout::source_checkout(temp.path.clone());
        let manager = ProcessManager::new(layout);
        let err = manager.backend_command("api").err().expect("missing run.py");

        assert!(err.contains("Backend source entry point not found"));
    }
}
