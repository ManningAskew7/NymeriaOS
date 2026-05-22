mod auto_config;
mod commands;
mod process_manager;
mod tray;

use process_manager::ProcessManager;
use std::sync::Arc;
use tauri::Emitter;

pub struct AppState {
    pub process_manager: Option<Arc<ProcessManager>>,
    pub api_key: String,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Default: client-only. The frontend connects to a remote backend (e.g. a
    // VPS) configured via the Setup Wizard or settings panel. Set
    // NYMERIA_SPAWN_BACKEND=1 to opt back into source-checkout dev mode, where
    // Tauri spawns the local Python API/worker from the checkout's run.py.
    // NYMERIA_CLIENT_ONLY=1 is still honored as an explicit override; it is
    // redundant with the new default but kept so existing launch scripts work.
    let parse_flag = |name: &str| {
        std::env::var(name)
            .map(|v| matches!(v.as_str(), "1" | "true" | "TRUE"))
            .unwrap_or(false)
    };
    let spawn_backend = parse_flag("NYMERIA_SPAWN_BACKEND");
    let force_client_only = parse_flag("NYMERIA_CLIENT_ONLY");

    let (pm, api_key) = if !spawn_backend || force_client_only {
        (None, String::new())
    } else {
        match ProcessManager::detect_runtime_layout() {
            Ok(layout) => {
                let key = auto_config::ensure_env_file(layout.backend_root()).unwrap_or_default();
                let manager = Arc::new(ProcessManager::new(layout));
                (Some(manager), key)
            }
            Err(_) => {
                // No source checkout detected; fall back to client-only
                (None, String::new())
            }
        }
    };

    let state = AppState {
        process_manager: pm.clone(),
        api_key,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(state)
        .setup(move |app| {
            // Setup system tray
            tray::setup_tray(app)?;

            let app_handle = app.handle().clone();

            if let Some(pm_clone) = pm.clone() {
                // Source-checkout dev mode: spawn backend processes in a background thread.
                std::thread::spawn(move || {
                    let _ = app_handle.emit("backend-status", "starting");

                    match pm_clone.start_api() {
                        Ok(()) => {}
                        Err(e) => {
                            let msg = format!("failed:{}", e);
                            let _ = app_handle.emit("backend-status", &msg);
                            return;
                        }
                    }

                    // Wait for API readiness. Dependency imports and database
                    // initialization can still be slow in source checkout dev.
                    match pm_clone.wait_for_api_ready(60) {
                        Ok(()) => {
                            let _ = app_handle.emit("backend-status", "ready");

                            if let Err(e) = pm_clone.start_worker() {
                                eprintln!("Worker failed to start: {}", e);
                            }
                        }
                        Err(e) => {
                            let msg = format!("failed:{}", e);
                            let _ = app_handle.emit("backend-status", &msg);
                        }
                    }
                });
            } else {
                // Client-only mode: no local backend to manage.
                let _ = app_handle.emit("backend-status", "client-only");
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_auto_config,
            commands::get_backend_status,
            commands::start_cliproxy,
            commands::stop_cliproxy,
            commands::get_cliproxy_status,
            commands::cliproxy_login,
        ])
        .on_window_event(|window, event| {
            // Hide window on close instead of quitting (tray keeps running)
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
