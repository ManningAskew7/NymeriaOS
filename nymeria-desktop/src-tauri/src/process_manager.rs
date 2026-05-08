//! Child process lifecycle management for Nymeria backend, worker, and CLIProxy.
//!
//! Uses Windows Job Objects to guarantee child processes die when the parent exits,
//! even on crash. All processes are spawned without console windows.

use shared_child::SharedChild;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
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

    fn bundled_resource(resource_root: PathBuf) -> Result<Self, String> {
        Ok(Self {
            project_root: resource_root,
            backend_root: default_user_project_root()?,
        })
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

    /// Walk up from the executable's location to find either a source checkout
    /// or a bundled Tauri resource root containing the PyInstaller backend.
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
            if has_bundled_backend(&path) {
                return RuntimeLayout::bundled_resource(path);
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

        let exe_parent = exe
            .parent()
            .ok_or("Cannot get exe parent dir")?
            .to_path_buf();
        for candidate in bundled_resource_candidates(&exe_parent) {
            if has_bundled_backend(&candidate) {
                return RuntimeLayout::bundled_resource(candidate);
            }
        }

        Err(
            "Could not find NymeriaOS root. \
             Ensure the app is located within the project tree, bundled with \
             Nymeria resources, or set NYMERIA_PROJECT_ROOT."
                .to_string(),
        )
    }

    /// Spawn the backend API server (`nymeria-backend.exe api`).
    pub fn start_api(&self) -> Result<(), String> {
        let backend_exe = self.backend_exe_path()?;
        std::fs::create_dir_all(&self.backend_root)
            .map_err(|e| format!("Failed to create backend runtime root: {}", e))?;

        let mut command = Command::new(&backend_exe);
        command
            .arg("api")
            .current_dir(&self.backend_root)
            .env("NYMERIA_PROJECT_ROOT", &self.backend_root);

        let child = spawn_no_window(&mut command)
            .map_err(|e| format!("Failed to spawn backend API: {}", e))?;

        let shared = self.wrap_child(child)?;
        *self.api_process.lock().unwrap() = Some(shared);
        Ok(())
    }

    /// Spawn the worker (ticker daemon).
    pub fn start_worker(&self) -> Result<(), String> {
        let backend_exe = self.backend_exe_path()?;
        std::fs::create_dir_all(&self.backend_root)
            .map_err(|e| format!("Failed to create backend runtime root: {}", e))?;

        let mut command = Command::new(&backend_exe);
        command
            .arg("worker")
            .current_dir(&self.backend_root)
            .env("NYMERIA_PROJECT_ROOT", &self.backend_root);

        let child = spawn_no_window(&mut command)
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

        let mut command = Command::new(&cliproxy_exe);
        command
            .arg("-config")
            .arg(&config_path)
            .current_dir(&cliproxy_dir);

        let child = spawn_no_window(&mut command)
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
            "Backend executable not found at {}. Build the PyInstaller backend first.",
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

fn is_source_checkout_root(path: &Path) -> bool {
    path.join("Nymeria").join("run.py").exists()
        && path.join("nymeria-desktop").is_dir()
}

fn has_bundled_backend(path: &Path) -> bool {
    path.join("Nymeria")
        .join("dist")
        .join("nymeria-backend.exe")
        .is_file()
}

fn bundled_resource_candidates(exe_parent: &Path) -> Vec<PathBuf> {
    vec![
        exe_parent.join("resources"),
        exe_parent.to_path_buf(),
        exe_parent.join("_up_"),
    ]
}

fn default_user_project_root() -> Result<PathBuf, String> {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .ok_or_else(|| "Cannot determine user profile directory".to_string())?;
    Ok(PathBuf::from(home).join(".nymeria"))
}

fn spawn_no_window(command: &mut Command) -> std::io::Result<Child> {
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    command.spawn()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
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
    fn recognizes_bundled_backend_resource_root() {
        let temp = TempTree::new();
        touch(
            &temp
                .path
                .join("Nymeria")
                .join("dist")
                .join("nymeria-backend.exe"),
        );

        assert!(has_bundled_backend(&temp.path));
    }

    #[test]
    fn bundled_backend_path_matches_tauri_resource_target() {
        let temp = TempTree::new();
        let backend_exe = temp
            .path
            .join("Nymeria")
            .join("dist")
            .join("nymeria-backend.exe");
        touch(&backend_exe);

        let layout = RuntimeLayout::bundled_resource(temp.path.clone()).expect("bundled layout");
        let manager = ProcessManager::new(layout);

        assert_eq!(manager.backend_exe_path().expect("backend path"), backend_exe);
    }

    #[test]
    fn source_backend_path_matches_pyinstaller_output() {
        let temp = TempTree::new();
        let backend_exe = temp
            .path
            .join("Nymeria")
            .join("dist")
            .join("nymeria-backend.exe");
        touch(&backend_exe);

        let layout = RuntimeLayout::source_checkout(temp.path.clone());
        let manager = ProcessManager::new(layout);

        assert_eq!(manager.backend_exe_path().expect("backend path"), backend_exe);
    }

    #[test]
    fn checks_expected_tauri_resource_candidate_order() {
        let exe_parent = PathBuf::from("C:/Program Files/Nymeria");

        assert_eq!(
            bundled_resource_candidates(&exe_parent),
            vec![
                exe_parent.join("resources"),
                exe_parent.clone(),
                exe_parent.join("_up_"),
            ]
        );
    }
}
