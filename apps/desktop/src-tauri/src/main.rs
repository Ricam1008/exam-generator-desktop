use std::{
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
};

use tauri::Manager;

struct BackendState(Mutex<Option<Child>>);

impl BackendState {
    fn stop(&self) {
        if let Ok(mut child) = self.0.lock() {
            if let Some(mut process) = child.take() {
                let _ = process.kill();
                let _ = process.wait();
            }
        }
    }
}

impl Drop for BackendState {
    fn drop(&mut self) {
        self.stop();
    }
}

fn backend_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let dev_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../backend");
    if dev_dir.exists() {
        return Ok(dev_dir);
    }
    let resource_dir = app.path().resource_dir().map_err(|err| err.to_string())?;
    let bundled_dir = resource_dir.join("backend");
    if bundled_dir.exists() {
        return Ok(bundled_dir);
    }
    Err("Could not find the bundled Python backend.".to_string())
}

fn sidecar_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "exam-generator-backend.exe"
    } else {
        "exam-generator-backend"
    }
}

fn bundled_backend(app: &tauri::AppHandle) -> Option<PathBuf> {
    let resource_dir = app.path().resource_dir().ok()?;
    let exe = resource_dir.join("sidecar-bin").join(sidecar_name());
    if exe.exists() {
        return Some(exe);
    }
    let mut stack = vec![resource_dir];
    while let Some(dir) = stack.pop() {
        let entries = std::fs::read_dir(dir).ok()?;
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.file_name().and_then(|name| name.to_str()) == Some(sidecar_name()) {
                return Some(path);
            }
        }
    }
    None
}

#[tauri::command]
fn start_backend(app: tauri::AppHandle, state: tauri::State<BackendState>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|err| err.to_string())?;
    if let Some(child) = guard.as_mut() {
        if child.try_wait().map_err(|err| err.to_string())?.is_none() {
            return Ok(());
        }
    }

    let child = if let Some(exe) = bundled_backend(&app) {
        Command::new(&exe)
            .arg("serve")
            .arg("--port")
            .arg("8766")
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|err| format!("Could not start bundled backend from {}: {err}", exe.display()))?
    } else {
        if !cfg!(debug_assertions) {
            return Err("Could not find the bundled backend executable.".to_string());
        }
        let dir = backend_dir(&app)?;
        Command::new("python3")
            .arg("-m")
            .arg("exam_backend.cli")
            .arg("serve")
            .arg("--port")
            .arg("8766")
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .env("PYTHONPATH", &dir)
            .current_dir(&dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|err| format!("Could not start Python backend from {}: {err}", dir.display()))?
    };

    *guard = Some(child);
    Ok(())
}

#[tauri::command]
fn stop_backend(state: tauri::State<BackendState>) {
    state.stop();
}

fn main() {
    let app = tauri::Builder::default()
        .manage(BackendState(Mutex::new(None)))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![start_backend, stop_backend])
        .build(tauri::generate_context!())
        .expect("error while building Exam Generator Desktop");

    app.run(|app_handle, event| match event {
        tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } => {
            app_handle.state::<BackendState>().stop();
        }
        tauri::RunEvent::WindowEvent {
            event: tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed,
            ..
        } => {
            app_handle.state::<BackendState>().stop();
        }
        _ => {}
    });
}
