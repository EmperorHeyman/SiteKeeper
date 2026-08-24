// Sitekeeper desktop shell.
//
// Mirrors the RaplMail shell: pick a free loopback port and a per-launch
// shared-secret token, hand both to the bundled Python backend as a sidecar,
// and expose them to the webview through one command. The frontend never
// hardcodes a port and the backend refuses any request without the token.
//
// For development, set MYSQLRUNNER_DEV_BASE (and optionally
// MYSQLRUNNER_DEV_TOKEN) to point at a backend you started yourself with
// `python backend/run.py`; the shell then skips spawning the sidecar.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpListener;
use std::sync::Mutex;

use rand::Rng;
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// Where the backend lives and how to authenticate to it.
#[derive(Clone, serde::Serialize)]
struct BackendInfo {
    base: String,
    token: String,
}

/// Holds the spawned backend process so it can be killed on exit.
struct BackendProcess(Mutex<Option<CommandChild>>);

#[tauri::command]
fn backend_info(state: State<'_, BackendInfo>) -> BackendInfo {
    state.inner().clone()
}

/// Ask the OS for an unused loopback port.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|listener| listener.local_addr())
        .map(|addr| addr.port())
        .expect("no free loopback port available")
}

/// 32 hex characters of per-launch shared secret.
fn random_token() -> String {
    let mut rng = rand::thread_rng();
    (0..32)
        .map(|_| std::char::from_digit(rng.gen_range(0..16), 16).unwrap())
        .collect()
}

fn main() {
    // A developer-provided backend wins over spawning our own.
    let dev_base = std::env::var("MYSQLRUNNER_DEV_BASE").ok();
    let dev_token = std::env::var("MYSQLRUNNER_DEV_TOKEN").unwrap_or_default();

    let (base, token, spawn_sidecar) = match dev_base {
        Some(base) => (base, dev_token, false),
        None => {
            let port = free_port();
            (format!("http://127.0.0.1:{port}"), random_token(), true)
        }
    };
    let port = base
        .rsplit(':')
        .next()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(8766);

    let info = BackendInfo {
        base: base.clone(),
        token: token.clone(),
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Second launch: focus the window we already have instead of
            // starting a rival backend on another port.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(info)
        .manage(BackendProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![backend_info])
        .setup(move |app| {
            if !spawn_sidecar {
                return Ok(());
            }
            let sidecar = app
                .shell()
                .sidecar("mysqlrunner-backend")?
                .env("MYSQLRUNNER_PORT", port.to_string())
                .env("MYSQLRUNNER_TOKEN", token.clone())
                .env("MYSQLRUNNER_VERSION", app.package_info().version.to_string());

            let (mut rx, child) = sidecar.spawn()?;
            app.state::<BackendProcess>()
                .0
                .lock()
                .unwrap()
                .replace(child);

            // Surface backend stderr to the frontend so the Debug view can show
            // why a launch failed instead of hanging on a blank window.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                use tauri_plugin_shell::process::CommandEvent;
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stderr(line) => {
                            let text = String::from_utf8_lossy(&line).to_string();
                            let _ = handle.emit("backend-log", text);
                        }
                        CommandEvent::Terminated(payload) => {
                            let _ = handle.emit("backend-exit", payload.code);
                            break;
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Never leave an orphaned backend holding the vault key in memory.
                if let Some(state) = window.app_handle().try_state::<BackendProcess>() {
                    if let Some(child) = state.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Sitekeeper");
}
