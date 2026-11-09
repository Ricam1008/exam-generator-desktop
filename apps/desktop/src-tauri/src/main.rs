use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{mpsc, Mutex},
    time::{Duration, Instant},
};
use serde::{Deserialize, Serialize};
use tauri::Manager;

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Connection { url: String, token: String }
struct Backend { child: Child, connection: Connection }
struct BackendState(Mutex<Option<Backend>>);
impl BackendState {
    fn stop(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut backend) = guard.take() {
                if let Some(stdin) = backend.child.stdin.as_mut() { let _ = stdin.write_all(b"shutdown\n"); }
                let deadline = Instant::now() + Duration::from_secs(8);
                while Instant::now() < deadline {
                    if backend.child.try_wait().ok().flatten().is_some() { return; }
                    std::thread::sleep(Duration::from_millis(50));
                }
                let _ = backend.child.kill();
                let _ = backend.child.wait();
            }
        }
    }
}
impl Drop for BackendState { fn drop(&mut self) { self.stop(); } }

#[tauri::command]
fn start_backend(app: tauri::AppHandle, state: tauri::State<BackendState>) -> Result<Connection, String> {
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    if let Some(backend) = guard.as_mut() {
        if backend.child.try_wait().map_err(|e| e.to_string())?.is_none() { return Ok(backend.connection.clone()); }
    }
    let log_dir = app.path().app_log_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    let log_path = log_dir.join("backend-startup.log");
    let log = OpenOptions::new().create(true).append(true).open(&log_path).map_err(|e| e.to_string())?;
    let bundled = app.path().resource_dir().map_err(|e| e.to_string())?.join("bin/exam-backend-v2/exam-backend-v2");
    let mut command;
    if bundled.is_file() {
        command = Command::new(bundled);
    } else if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
        let packaged_python = root.join(".venv-package/bin/python");
        let dev_python = root.join(".venv/bin/python");
        let python = if packaged_python.is_file() { packaged_python } else if dev_python.is_file() { dev_python } else { PathBuf::from("python3") };
        command = Command::new(python);
        command.arg("-m").arg("exam_backend.cli").env("PYTHONPATH", root.join("backend"));
    } else {
        return Err("Der gebündelte Dienst fehlt. Bitte die App erneut installieren.".into());
    }
    let mut child = command.arg("serve").arg("--port").arg("0")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::from(log.try_clone().map_err(|e| e.to_string())?))
        .spawn().map_err(|e| format!("Lokaler Dienst konnte nicht starten: {e}. Diagnose: {}", log_path.display()))?;
    let stdout = child.stdout.take().ok_or("Der Dienst hat keinen Startkanal.")?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut lines = BufReader::new(stdout).lines();
        let result = lines.next().ok_or_else(|| "Dienst wurde vor dem Start beendet.".to_string())
            .and_then(|line| line.map_err(|e| e.to_string()))
            .and_then(|line| serde_json::from_str::<Connection>(&line).map_err(|e| e.to_string()));
        let _ = sender.send(result);
        let mut log = log;
        for line in lines.map_while(Result::ok) { let _ = writeln!(log, "{line}"); }
    });
    let connection = match receiver.recv_timeout(Duration::from_secs(30)) {
        Ok(Ok(connection)) => connection,
        other => {
            let _ = child.kill(); let _ = child.wait();
            return Err(format!("Dienst konnte nicht starten ({other:?}). Diagnose: {}", log_path.display()));
        }
    };
    *guard = Some(Backend { child, connection: connection.clone() });
    Ok(connection)
}
#[tauri::command]
fn stop_backend(state: tauri::State<BackendState>) { state.stop(); }
fn main() {
    let app = tauri::Builder::default().manage(BackendState(Mutex::new(None)))
        .plugin(tauri_plugin_dialog::init()).plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![start_backend, stop_backend])
        .build(tauri::generate_context!()).expect("Exam Generator konnte nicht starten");
    app.run(|app, event| match event {
        tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } => app.state::<BackendState>().stop(),
        tauri::RunEvent::WindowEvent { event: tauri::WindowEvent::Destroyed, .. } => app.state::<BackendState>().stop(),
        _ => {}
    });
}
