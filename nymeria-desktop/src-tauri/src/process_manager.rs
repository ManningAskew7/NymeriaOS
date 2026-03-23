//! Child process lifecycle management for Nymeria backend, worker, and CLIProxy.
//!
//! Uses Windows Job Objects to guarantee child processes die when the parent exits,
//! even on crash. All processes are spawned without console windows.

use shared_child::SharedChild;
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

const CREATE_NO_WINDOW: u32 = 0x08000000;

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
    api_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    worker_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    cliproxy_process: Arc<Mutex<Option<Arc<SharedChild>>>>,
    #[cfg(windows)]
    job_object: Option<JobObject>,
}

impl ProcessManager {
    pub fn new(project_root: PathBuf) -> Self {
        Self {
            project_root,
            api_process: Arc::new(Mutex::new(None)),
            worker_process: Arc::new(Mutex::new(None)),
            cliproxy_process: Arc::new(Mutex::new(None)),
            #[cfg(windows)]
            job_object: JobObject::new(),
        }
    }

    /// Walk up from the executable's location to find the NymeriaOS root.
    /// Looks for the `Nymeria/` and `nymeria-desktop/` directories as markers.
    pub fn detect_project_root() -> Result<PathBuf, String> {
        // Check env var override first
        if let Ok(root) = std::env::var("NYMERIA_PROJECT_ROOT") {
            let path = PathBuf::from(&root);
            // NYMERIA_PROJECT_ROOT might point to the Nymeria/ subdir or the repo root
            if path.join("nymeria").exists() && path.join("run.py").exists() {
                // Points to Nymeria/ subdir — go up one level
                if let Some(parent) = path.parent() {
                    return Ok(parent.to_path_buf());
                }
            }
            if path.join("Nymeria").exists() {
                return Ok(path);
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
            if dir.join("Nymeria").is_dir() && dir.join("nymeria-desktop").is_dir() {
                return Ok(dir);
            }
            if !dir.pop() {
                break;
            }
        }

        Err(
            "Could not find NymeriaOS root. \
             Ensure the app is located within the project tree, \
             or set NYMERIA_PROJECT_ROOT."
                .to_string(),
        )
    }

    /// Spawn the backend API server (`nymeria-backend.exe api`).
    pub fn start_api(&self) -> Result<(), String> {
        let backend_exe = self.backend_exe_path()?;
        let nymeria_dir = self.project_root.join("Nymeria");

        let child = Command::new(&backend_exe)
            .arg("api")
            .current_dir(&nymeria_dir)
            .env("NYMERIA_PROJECT_ROOT", &nymeria_dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Failed to spawn backend API: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.api_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Spawn the worker (ticker daemon).
    pub fn start_worker(&self) -> Result<(), String> {
        let backend_exe = self.backend_exe_path()?;
        let nymeria_dir = self.project_root.join("Nymeria");

        let child = Command::new(&backend_exe)
            .arg("worker")
            .current_dir(&nymeria_dir)
            .env("NYMERIA_PROJECT_ROOT", &nymeria_dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Failed to spawn worker: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.worker_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Start the CLIProxy (`cliproxy.exe`).
    pub fn start_cliproxy(&self) -> Result<(), String> {
        // Don't start if already running
        if Self::is_process_alive(&self.cliproxy_process) {
            return Ok(());
        }

        let cliproxy_dir = self.project_root.join("CLIProxyAPI-main");
        let cliproxy_exe = cliproxy_dir.join("cliproxy.exe");

        if !cliproxy_exe.exists() {
            return Err(format!(
                "CLIProxy not found at {}",
                cliproxy_exe.display()
            ));
        }

        let config_path = cliproxy_dir.join("config.yaml");

        let child = Command::new(&cliproxy_exe)
            .arg("-config")
            .arg(&config_path)
            .current_dir(&cliproxy_dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("Failed to spawn CLIProxy: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.cliproxy_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Stop the CLIProxy process.
    pub fn stop_cliproxy(&self) -> Result<(), String> {
        Self::kill_process(&self.cliproxy_process);
        Ok(())
    }

    /// Check if the CLIProxy process is alive.
    pub fn is_cliproxy_running(&self) -> bool {
        Self::is_process_alive(&self.cliproxy_process)
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

    fn backend_exe_path(&self) -> Result<PathBuf, String> {
        let exe = self.project_root.join("Nymeria").join("dist").join("nymeria-backend.exe");
        if exe.exists() {
            return Ok(exe);
        }

        // Fallback: try running via Python directly (dev mode)
        Err(format!(
            "Backend executable not found at {}. Run build_exe.bat first.",
            exe.display()
        ))
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
