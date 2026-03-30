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
    // Detect project root — if not found, run in client-only mode
    let (pm, api_key) = match ProcessManager::detect_project_root() {
        Ok(project_root) => {
            let key = auto_config::ensure_env_file(&project_root)
                .unwrap_or_default();
            let manager = Arc::new(ProcessManager::new(project_root));
            (Some(manager), key)
        }
        Err(_) => {
            // Client-only mode: no local backend, frontend connects to remote
            (None, String::new())
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
                // Self-contained mode: spawn backend processes in a background thread
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

                    // Wait for API to be ready (up to 60 seconds — PyInstaller can be slow on first run)
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
                // Client-only mode: no local backend to manage, signal ready immediately
                let _ = app_handle.emit("backend-status", "ready");
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
            commands::apply_cliproxy_base_url,
            commands::remove_cliproxy_base_url,
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
