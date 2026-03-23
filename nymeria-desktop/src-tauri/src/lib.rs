mod auto_config;
mod commands;
mod process_manager;
mod tray;

use process_manager::ProcessManager;
use std::sync::Arc;
use tauri::Emitter;

pub struct AppState {
    pub process_manager: Arc<ProcessManager>,
    pub api_key: String,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Detect project root
    let project_root = ProcessManager::detect_project_root()
        .expect("Could not find NymeriaOS root directory");

    // Auto-configure .env and get API key
    let api_key = auto_config::ensure_env_file(&project_root)
        .expect("Failed to configure .env file");

    let pm = Arc::new(ProcessManager::new(project_root));

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

            // Spawn backend processes in a background thread
            let pm_clone = pm.clone();
            let app_handle = app.handle().clone();

            std::thread::spawn(move || {
                // Emit starting status
                let _ = app_handle.emit("backend-status", "starting");

                // Start API
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

                        // Start worker after API is healthy
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
