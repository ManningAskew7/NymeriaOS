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
    if state.process_manager.is_none() {
        return Err("Client-only mode — configure backend URL in settings".to_string());
    }
    Ok(AutoConfig {
        api_url: "http://localhost:8000".to_string(),
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
    let running = state
        .process_manager
        .as_ref()
        .map(|pm| pm.is_cliproxy_running())
        .unwrap_or(false);

    if !running {
        return CLIProxyStatus {
            running: false,
            sessions: vec![],
        };
    }

    let sessions = match query_cliproxy_sessions() {
        Ok(s) => s,
        Err(_) => vec![],
    };

    CLIProxyStatus { running, sessions }
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
