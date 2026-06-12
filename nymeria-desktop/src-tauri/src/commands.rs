//! Tauri commands exposed to the frontend via `invoke()`.

use crate::AppState;
use serde::Serialize;
use std::process::{Child, Command};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const CLIPROXY_HOST_BASE_URL: &str = "http://127.0.0.1:8318";
const CLIPROXY_CONTAINER_NAME: &str = "cli-proxy-api-latest";

#[derive(Serialize)]
pub struct AutoConfig {
    pub api_url: String,
    pub api_key: String,
}

#[derive(Serialize)]
pub struct BackendStatus {
    pub api: String,
    pub worker: String,
}

#[derive(Serialize)]
pub struct CLIProxyStatus {
    pub running: bool,
    pub base_url: String,
    pub detail: String,
    pub sessions: Vec<CLIProxySession>,
}

#[derive(Serialize)]
pub struct CLIProxySession {
    pub provider: String,
    pub email: String,
}

/// Get the auto-generated API configuration for the frontend.
#[tauri::command]
pub fn get_auto_config(state: tauri::State<'_, AppState>) -> Result<AutoConfig, String> {
    let Some(pm) = state.process_manager.as_ref() else {
        return Err("Client-only mode: configure backend URL in settings".to_string());
    };
    Ok(AutoConfig {
        // Follows the backend root's API_PORT; the spawned backend resolves
        // its port from the same config files.
        api_url: pm.local_api_base_url(),
        api_key: state.api_key.clone(),
    })
}

/// Get the current backend process status.
#[tauri::command]
pub fn get_backend_status(state: tauri::State<'_, AppState>) -> BackendStatus {
    match &state.process_manager {
        Some(pm) => BackendStatus {
            api: if pm.is_api_running() {
                "running".to_string()
            } else {
                "stopped".to_string()
            },
            worker: if pm.is_worker_running() {
                "running".to_string()
            } else {
                "stopped".to_string()
            },
        },
        None => BackendStatus {
            api: "external".to_string(),
            worker: "external".to_string(),
        },
    }
}

/// Start the CLIProxy process.
#[tauri::command]
pub fn start_cliproxy(state: tauri::State<'_, AppState>) -> Result<(), String> {
    state
        .process_manager
        .as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?
        .start_cliproxy()
}

/// Stop the CLIProxy process.
#[tauri::command]
pub fn stop_cliproxy(state: tauri::State<'_, AppState>) -> Result<(), String> {
    state
        .process_manager
        .as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?
        .stop_cliproxy()
}

/// Get CLIProxy status and active OAuth sessions.
#[tauri::command]
pub fn get_cliproxy_status(state: tauri::State<'_, AppState>) -> CLIProxyStatus {
    let Some(pm) = state.process_manager.as_ref() else {
        return CLIProxyStatus {
            running: false,
            base_url: CLIPROXY_HOST_BASE_URL.to_string(),
            detail: "Client-only mode; local CLIProxy process management is unavailable.".to_string(),
            sessions: vec![],
        };
    };

    let running = pm.is_cliproxy_running();

    if !running {
        return CLIProxyStatus {
            running: false,
            base_url: CLIPROXY_HOST_BASE_URL.to_string(),
            detail: format!(
                "No CLIProxy endpoint is reachable from this desktop at {}.",
                CLIPROXY_HOST_BASE_URL
            ),
            sessions: vec![],
        };
    }

    let sessions = match query_cliproxy_sessions() {
        Ok(s) => s,
        Err(_) => vec![],
    };

    CLIProxyStatus {
        running,
        base_url: CLIPROXY_HOST_BASE_URL.to_string(),
        detail: "CLIProxy endpoint is reachable from this desktop.".to_string(),
        sessions,
    }
}

/// Initiate OAuth login for a provider against the current pinned Docker container.
#[tauri::command]
pub fn cliproxy_login(state: tauri::State<'_, AppState>, provider: String) -> Result<(), String> {
    let pm = state
        .process_manager
        .as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?;

    if !pm.is_cliproxy_running() {
        pm.start_cliproxy()?;
    }

    let flag = match provider.as_str() {
        "claude" => "--claude-login",
        "openai" => "--codex-device-login",
        _ => return Err(format!("Unknown provider: {}", provider)),
    };

    let mut command = Command::new("docker");
    command
        .arg("exec")
        .arg("-i")
        .arg(CLIPROXY_CONTAINER_NAME)
        .arg("./CLIProxyAPI")
        .arg(flag)
        .arg("--no-browser");

    spawn_no_window(&mut command).map_err(|e| format!("Failed to start login: {}", e))?;

    Ok(())
}

/// Query CLIProxy management API for active OAuth sessions.
fn query_cliproxy_sessions() -> Result<Vec<CLIProxySession>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .map_err(|e| format!("{}", e))?;

    let resp = client
        .get(format!(
            "{}/v0/management/oauth/sessions",
            CLIPROXY_HOST_BASE_URL
        ))
        .send()
        .map_err(|e| format!("{}", e))?;

    if !resp.status().is_success() {
        return Ok(vec![]);
    }

    let body: serde_json::Value = resp.json().map_err(|e| format!("{}", e))?;

    let mut sessions = vec![];
    if let Some(arr) = body.as_array() {
        for item in arr {
            let provider = item
                .get("type")
                .or_else(|| item.get("provider"))
                .and_then(|v| v.as_str())
                .unwrap_or("unknown")
                .to_string();
            let email = item
                .get("email")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            sessions.push(CLIProxySession { provider, email });
        }
    }

    Ok(sessions)
}

fn spawn_no_window(command: &mut Command) -> std::io::Result<Child> {
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    command.spawn()
}

// ---------------------------------------------------------------------------
// OS keychain (H-7): keep backend bearer tokens out of plaintext localStorage.
//
// The frontend stores secret material (the live API token + saved-connection
// tokens) here instead of localStorage. Backed by the platform secret store:
// macOS Keychain, Windows Credential Manager, and the Linux Secret Service.
// Keys are namespaced under the app's bundle id; the frontend uses a small
// wrapper (`secureStorage.ts`) that falls back to localStorage when these
// commands are unavailable (e.g. a browser dev preview).
// ---------------------------------------------------------------------------

const KEYCHAIN_SERVICE: &str = "com.nymeriaos.desktop";

#[tauri::command]
pub fn keychain_set(key: String, value: String) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYCHAIN_SERVICE, &key).map_err(|e| e.to_string())?;
    entry.set_password(&value).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn keychain_get(key: String) -> Result<Option<String>, String> {
    let entry = keyring::Entry::new(KEYCHAIN_SERVICE, &key).map_err(|e| e.to_string())?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
pub fn keychain_delete(key: String) -> Result<(), String> {
    let entry = keyring::Entry::new(KEYCHAIN_SERVICE, &key).map_err(|e| e.to_string())?;
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}
