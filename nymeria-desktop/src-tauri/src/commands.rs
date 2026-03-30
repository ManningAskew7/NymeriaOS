//! Tauri commands exposed to the frontend via `invoke()`.

use crate::AppState;
use serde::Serialize;
use std::process::Command;
use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x08000000;

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
    state.process_manager.as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?
        .start_cliproxy()
}

/// Stop the CLIProxy process.
#[tauri::command]
pub fn stop_cliproxy(state: tauri::State<'_, AppState>) -> Result<(), String> {
    state.process_manager.as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?
        .stop_cliproxy()
}

/// Get CLIProxy status and active OAuth sessions.
#[tauri::command]
pub fn get_cliproxy_status(state: tauri::State<'_, AppState>) -> CLIProxyStatus {
    let running = state.process_manager.as_ref()
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

/// Initiate OAuth login for a provider (opens browser).
#[tauri::command]
pub fn cliproxy_login(state: tauri::State<'_, AppState>, provider: String) -> Result<(), String> {
    let pm = state.process_manager.as_ref()
        .ok_or_else(|| "Not available in client-only mode".to_string())?;
    let project_root = pm.project_root();
    let cliproxy_dir = project_root.join("CLIProxyAPI-main");
    let cliproxy_exe = cliproxy_dir.join("cliproxy.exe");

    if !cliproxy_exe.exists() {
        return Err("CLIProxy executable not found".to_string());
    }

    let flag = match provider.as_str() {
        "claude" => "-claude-login",
        "openai" => "-codex-login",
        _ => return Err(format!("Unknown provider: {}", provider)),
    };

    let config_path = cliproxy_dir.join("config.yaml");

    Command::new(&cliproxy_exe)
        .arg(flag)
        .arg("-config")
        .arg(&config_path)
        .current_dir(&cliproxy_dir)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| format!("Failed to start login: {}", e))?;

    Ok(())
}

/// Apply CLIProxy as the LLM base URL in the backend settings.
#[tauri::command]
pub fn apply_cliproxy_base_url(state: tauri::State<'_, AppState>) -> Result<(), String> {
    if state.process_manager.is_none() {
        return Err("Not available in client-only mode".to_string());
    }

    let client = reqwest::blocking::Client::new();

    let api_key = &state.api_key;
    let body = serde_json::json!({
        "llm_base_url": "http://127.0.0.1:8317/v1"
    });

    let resp = client
        .patch("http://127.0.0.1:8000/settings")
        .header("Authorization", format!("Bearer {}", api_key))
        .json(&body)
        .send()
        .map_err(|e| format!("Failed to update settings: {}", e))?;

    if resp.status().is_success() {
        Ok(())
    } else {
        Err(format!("Settings update failed: {}", resp.status()))
    }
}

/// Remove the CLIProxy base URL override from backend settings.
#[tauri::command]
pub fn remove_cliproxy_base_url(state: tauri::State<'_, AppState>) -> Result<(), String> {
    if state.process_manager.is_none() {
        return Err("Not available in client-only mode".to_string());
    }

    let client = reqwest::blocking::Client::new();

    let api_key = &state.api_key;
    let body = serde_json::json!({
        "llm_base_url": null
    });

    let resp = client
        .patch("http://127.0.0.1:8000/settings")
        .header("Authorization", format!("Bearer {}", api_key))
        .json(&body)
        .send()
        .map_err(|e| format!("Failed to update settings: {}", e))?;

    if resp.status().is_success() {
        Ok(())
    } else {
        Err(format!("Settings update failed: {}", resp.status()))
    }
}

/// Query CLIProxy management API for active OAuth sessions.
fn query_cliproxy_sessions() -> Result<Vec<CLIProxySession>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
        .map_err(|e| format!("{}", e))?;

    let resp = client
        .get("http://127.0.0.1:8317/v0/management/oauth/sessions")
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
