//! Tauri shell for Goblin on Linux.
//!
//! The shell owns one Goblin window and one Python engine process. The engine
//! speaks newline-delimited JSON on stdin and stdout; this file only moves
//! bytes between the web UI and that pipe.

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;

use tauri::{Emitter, Manager, State};

struct EngineState {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
}

impl EngineState {
    fn stop(&self) {
        if let Ok(mut stdin) = self.stdin.lock() {
            if let Some(mut handle) = stdin.take() {
                let frame = "{\"id\":\"app-exit\",\"method\":\"shutdown\",\"params\":{}}";
                let _ = handle.write_all(frame.as_bytes());
                let _ = handle.write_all(b"\n");
                let _ = handle.flush();
            }
        }
        if let Ok(mut child) = self.child.lock() {
            if let Some(mut process) = child.take() {
                let _ = process.kill();
                let _ = process.wait();
            }
        }
    }
}

fn data_home() -> PathBuf {
    let base = std::env::var("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
            PathBuf::from(home).join(".local").join("share")
        });
    base.join("goblin")
}

fn engine_root(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(root) = std::env::var("GOBLIN_ROOT") {
        return PathBuf::from(root);
    }
    if cfg!(debug_assertions) {
        let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        if let Some(parent) = manifest.parent() {
            return parent.to_path_buf();
        }
    }
    let resources = app
        .path()
        .resource_dir()
        .unwrap_or_else(|_| PathBuf::from("."));
    // Tauri keeps `../` bundle entries under an `_up_` directory.
    let nested = resources.join("_up_");
    if nested.join("Runtime").is_dir() {
        return nested;
    }
    resources
}

fn engine_python() -> String {
    std::env::var("GOBLIN_PYTHON").unwrap_or_else(|_| "python3".to_string())
}

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("Only https links can be opened".to_string());
    }
    Command::new("xdg-open")
        .arg(&url)
        .spawn()
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn spawn_engine(app: &tauri::AppHandle) -> Result<EngineState, String> {
    let root = engine_root(app);
    let home = data_home();
    std::fs::create_dir_all(&home).map_err(|error| error.to_string())?;
    let log_path = home.join("engine.log");
    let log = File::create(&log_path).map_err(|error| error.to_string())?;

    let mut command = Command::new(engine_python());
    command
        .arg(root.join("Runtime").join("goblin_bridge.py"))
        .arg("--home")
        .arg(&home)
        .arg("--manifest")
        .arg(root.join("product.json"))
        .arg("--application-home")
        .arg(&home)
        .env("GOBLIN_RESOURCES", root.join("Resources"))
        .env("GOBLIN_KEYCHAIN_HELPER", root.join("scripts").join("goblin-credentials"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log))
        .current_dir(&root);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start the engine: {error}"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "The engine has no stdin".to_string())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "The engine has no stdout".to_string())?;

    let app_handle = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            match line {
                Ok(text) => {
                    eprintln!("engine frame bytes={}", text.len());
                    if let Some(window) = app_handle.get_webview_window("main") {
                        let literal = serde_json::to_string(&text).unwrap_or_else(|_| "\"\"".to_string());
                        let script = format!("window.__goblinFrame && window.__goblinFrame({literal});");
                        if let Err(error) = window.eval(&script) {
                            eprintln!("engine eval failed: {error}");
                        }
                    } else {
                        eprintln!("engine frame dropped: no main window");
                    }
                }
                Err(_) => break,
            }
        }
        let _ = app_handle.emit("engine-exit", "");
    });

    Ok(EngineState {
        stdin: Mutex::new(Some(stdin)),
        child: Mutex::new(Some(child)),
    })
}

#[tauri::command]
fn engine_request(state: State<'_, EngineState>, frame: String) -> Result<(), String> {
    eprintln!("ui request bytes={}", frame.len());
    let mut guard = state
        .stdin
        .lock()
        .map_err(|_| "The engine lock was poisoned".to_string())?;
    let handle = guard
        .as_mut()
        .ok_or_else(|| "The engine is not running".to_string())?;
    handle
        .write_all(frame.as_bytes())
        .map_err(|error| error.to_string())?;
    handle.write_all(b"\n").map_err(|error| error.to_string())?;
    handle.flush().map_err(|error| error.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let resolver = app.asset_resolver();
            match resolver.get("index.html".to_string()) {
                Some(asset) => {
                    let text = String::from_utf8_lossy(&asset.bytes);
                    let main = resolver.get("main.js".to_string());
                    let api = resolver.get("api.js".to_string());
                    let main_text = main
                        .as_ref()
                        .map(|item| String::from_utf8_lossy(&item.bytes).to_string())
                        .unwrap_or_default();
                    let api_text = api
                        .as_ref()
                        .map(|item| String::from_utf8_lossy(&item.bytes).to_string())
                        .unwrap_or_default();
                    eprintln!(
                        "ui asset index bytes={} boot_error={} companion={} character_js={} main_bytes={} main_probe={} main_loaded={} api_frame_error={}",
                        asset.bytes.len(),
                        text.contains("boot-error"),
                        text.contains("companion"),
                        resolver.get("character.js".to_string()).is_some(),
                        main_text.len(),
                        main_text.contains("ui-probe"),
                        main_text.contains("Loaded "),
                        api_text.contains("frame_error")
                    );
                }
                None => eprintln!("ui asset index missing"),
            }
            let handle = app.handle().clone();
            let state = spawn_engine(&handle).map_err(|message| {
                Box::<dyn std::error::Error>::from(std::io::Error::new(
                    std::io::ErrorKind::Other,
                    message,
                ))
            })?;
            app.manage(state);
            let probe_handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(800));
                if let Err(error) = probe_handle.emit("ui-probe", "hello") {
                    eprintln!("ui probe emit failed: {error}");
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![engine_request, open_url])
        .build(tauri::generate_context!())
        .expect("Goblin could not build its window")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<EngineState>() {
                    state.stop();
                }
            }
        });
}
