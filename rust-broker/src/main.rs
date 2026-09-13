use memorycore_ai_broker::{
    Config, MAX_FRAME, MAX_PENDING, MAX_RESPONSE, Request, is_read, validate_request,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    process::Stdio,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{Mutex, Semaphore, mpsc},
    task::JoinSet,
    time::timeout,
};

const WAIT: Duration = Duration::from_secs(10);

async fn frame<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> Result<Option<Vec<u8>>, ()> {
    let mut result = Vec::new();
    loop {
        let buf = reader.fill_buf().await.map_err(|_| ())?;
        if buf.is_empty() {
            return if result.is_empty() { Ok(None) } else { Err(()) };
        }
        let end = buf.iter().position(|b| *b == b'\n').map(|i| i + 1);
        let n = end.unwrap_or(buf.len());
        if result.len() + n > limit {
            return Err(());
        }
        result.extend_from_slice(&buf[..n]);
        reader.consume(n);
        if end.is_some() {
            return Ok(Some(result));
        }
    }
}

struct Worker {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}
impl Worker {
    async fn start(c: &Config, role: &str) -> Result<Self, ()> {
        let mut command = if c.backend == "native" {
            let mut command = Command::new(std::env::current_exe().map_err(|_| ())?);
            command.args(["--native-worker", "--role", role]);
            command
        } else {
            let mut command = Command::new(&c.python);
            command
                .args(["-B", "-m", "scripts.rust_worker", "--role", role])
                .current_dir(&c.backend_root);
            command
        };
        command
            .env("RAYON_NUM_THREADS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(windows)]
        command.creation_flags(0x08000000);
        let mut child = command.spawn().map_err(|_| ())?;
        let input = child.stdin.take().ok_or(())?;
        let output = BufReader::new(child.stdout.take().ok_or(())?);
        let mut worker = Self {
            child,
            input,
            output,
        };
        let mut config = serde_json::to_vec(c).map_err(|_| ())?;
        config.push(b'\n');
        if config.len() > MAX_FRAME {
            return Err(());
        }
        timeout(WAIT, worker.input.write_all(&config))
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
        worker.input.flush().await.map_err(|_| ())?;
        let ready = timeout(WAIT, frame(&mut worker.output, MAX_RESPONSE))
            .await
            .map_err(|_| ())??
            .ok_or(())?;
        let ready: Value = serde_json::from_slice(&ready).map_err(|_| ())?;
        if ready.get("ready") != Some(&json!(true)) {
            return Err(());
        }
        Ok(worker)
    }
    async fn call(&mut self, request: &Request) -> Result<Value, ()> {
        let mut raw = serde_json::to_vec(request).map_err(|_| ())?;
        raw.push(b'\n');
        self.input.write_all(&raw).await.map_err(|_| ())?;
        self.input.flush().await.map_err(|_| ())?;
        let response = frame(&mut self.output, MAX_RESPONSE).await?.ok_or(())?;
        let value: Value = serde_json::from_slice(&response).map_err(|_| ())?;
        if value.get("session") != Some(&json!(request.session))
            || value.get("id") != Some(&json!(request.id))
        {
            return Err(());
        }
        Ok(value)
    }
    async fn stop(&mut self) {
        let _ = self.child.kill().await;
        let _ = self.child.wait().await;
    }
}

fn error(r: &Request, reason: &str, unknown: bool) -> Value {
    json!({"session":r.session,"id":r.id,"error":reason,"outcome_unknown":unknown})
}

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--backup") {
        if memorycore_ai_broker::native::backup::command().is_err() {
            eprintln!(
                "Encrypted backup rejected; validate source, destination and independent key."
            );
            std::process::exit(1);
        }
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--version") && std::env::args().len() == 2 {
        let cipher = rusqlite::Connection::open_in_memory().ok().and_then(|c| {
            c.query_row("PRAGMA cipher_version", [], |r| r.get::<_, String>(0))
                .ok()
        });
        println!(
            "{}",
            json!({"version":env!("CARGO_PKG_VERSION"),"sqlite":rusqlite::version(),"sqlcipher":cipher,"vector_search":true,"synthetic_only":true})
        );
        return;
    }
    if std::env::args().nth(1).as_deref() == Some("--native-worker") {
        if memorycore_ai_broker::native::service::worker().is_err() {
            eprintln!("Native worker stopped; validate host configuration.");
            std::process::exit(1);
        }
        return;
    }
    if [Some("--native-command"), Some("--init")].contains(&std::env::args().nth(1).as_deref()) {
        if memorycore_ai_broker::native::service::command().is_err() {
            eprintln!("Native command rejected; validate configuration, scope and request.");
            std::process::exit(1);
        }
        return;
    }
    if tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("runtime unavailable")
        .block_on(run())
        .is_err()
    {
        eprintln!("Rust broker stopped; verify synthetic host configuration or transport.");
        std::process::exit(1);
    }
}

async fn run() -> Result<(), ()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 || args[1] != "--config" {
        return Err(());
    }
    let path = std::fs::canonicalize(&args[2]).map_err(|_| ())?;
    if std::fs::metadata(&path).map_err(|_| ())?.len() > MAX_FRAME as u64 {
        return Err(());
    }
    let config: Config =
        serde_json::from_slice(&std::fs::read(&path).map_err(|_| ())?).map_err(|_| ())?;
    config.validate().map_err(|_| ())?;
    let mut workers = Vec::new();
    // Isolated workers allow hard deadlines without cancelling a live write in-process.
    for n in 0..=config.read_workers {
        workers.push(Arc::new(Mutex::new(Some(
            Worker::start(
                &config,
                if n == config.read_workers {
                    "write"
                } else {
                    "read"
                },
            )
            .await?,
        ))));
    }
    let allowed: HashSet<_> = config.sessions.iter().map(|s| s.id.clone()).collect();
    let mut scope_readers = BTreeMap::new();
    let mut preferred_readers = HashMap::new();
    for session in &config.sessions {
        let next = scope_readers.len() % config.read_workers;
        let index = *scope_readers.entry(session.scope.clone()).or_insert(next);
        preferred_readers.insert(session.id.clone(), index);
    }
    let busy = Arc::new(Mutex::new(HashSet::<String>::new()));
    let capacity = Arc::new(Semaphore::new(MAX_PENDING));
    let (out, mut output) = mpsc::channel::<Value>(MAX_PENDING);
    let output_task = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(value) = output.recv().await {
            let mut bytes = serde_json::to_vec(&value).map_err(|_| ())?;
            bytes.push(b'\n');
            stdout.write_all(&bytes).await.map_err(|_| ())?;
            stdout.flush().await.map_err(|_| ())?;
        }
        Ok::<(), ()>(())
    });
    let mut worker_pids = Vec::new();
    for worker in &workers {
        worker_pids.push(
            worker
                .lock()
                .await
                .as_ref()
                .ok_or(())?
                .child
                .id()
                .ok_or(())?,
        );
    }
    out.send(
        json!({"event":"ready","runtime":"rust","backend":if config.backend=="native"{"rust-native"}else{"python-compatibility"},"worker_pids":worker_pids,
        "read_workers":config.read_workers,"write_workers":1,"max_pending":MAX_PENDING,
        "synthetic_only":true,"native_chat_capture":false}),
    )
    .await
    .map_err(|_| ())?;
    let mut input = BufReader::new(tokio::io::stdin());
    let mut jobs = JoinSet::new();
    let readers = Arc::new(memorycore_ai_broker::scheduler::ReadPool::new(
        config.read_workers,
    ));
    let maintenance = Arc::new(Semaphore::new(1));
    let mut transport_failed = false;
    loop {
        while jobs.try_join_next().is_some() {}
        let raw = match frame(&mut input, MAX_FRAME).await {
            Ok(Some(raw)) => raw,
            Ok(None) => break,
            Err(()) => {
                transport_failed = true;
                break;
            }
        };
        let request: Request = match serde_json::from_slice(&raw) {
            Ok(r) => r,
            Err(_) => {
                out.send(json!({"error":"invalid_request"}))
                    .await
                    .map_err(|_| ())?;
                continue;
            }
        };
        if !validate_request(&request)
            || !allowed.contains(&request.session)
            || (config.backend != "native" && request.recovery.is_some())
        {
            out.send(error(&request, "invalid_request_or_session", false))
                .await
                .map_err(|_| ())?;
            continue;
        }
        let permit = match capacity.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => {
                out.send(error(&request, "queue_full", false))
                    .await
                    .map_err(|_| ())?;
                continue;
            }
        };
        if !busy.lock().await.insert(request.session.clone()) {
            out.send(error(&request, "session_busy", false))
                .await
                .map_err(|_| ())?;
            continue;
        }
        let read = is_read(&request);
        let preferred_reader = preferred_readers.get(&request.session).copied();
        let workers = workers.clone();
        let readers = readers.clone();
        let maintenance = maintenance.clone();
        let write_index = config.read_workers;
        let out = out.clone();
        let busy = busy.clone();
        jobs.spawn(async move {
            let _permit = permit;
            let queued = Instant::now();
            let assignment = timeout(WAIT, async {
                let maintenance_permit = if read && request.operation == "admin"
                    && matches!(request.arguments["action"].as_str(),Some("export"|"verify"|"inspect"|"knowledge-page")) {
                    Some(maintenance.acquire_owned().await.unwrap())
                } else { None };
                let lease = if read { readers.acquire_preferred(preferred_reader).await } else { None };
                (maintenance_permit,lease)
            }).await;
            let Ok((maintenance_permit, mut lease)) = assignment else {
                busy.lock().await.remove(&request.session);
                let mut response = error(&request,"queue_timeout",false);
                response["lane"] = json!(if read {"read"} else {"write"});
                let _ = out.send(response).await;
                return;
            };
            if read && lease.is_none() {
                busy.lock().await.remove(&request.session);
                let _ = out.send(error(&request, "worker_unavailable", false)).await;
                return;
            }
            let worker = workers[lease.as_ref().map_or(write_index,|l|l.index)].clone();
            let mut response = match timeout(WAIT, worker.lock()).await {
                Err(_) => error(&request, "queue_timeout", false),
                Ok(mut guard) => {
                    let started = Instant::now();
                    match guard.as_mut() {
                        None => error(&request, "worker_unavailable", false),
                        Some(child) => {
                            match timeout(WAIT, child.call(&request)).await {
                                Ok(Ok(mut response)) => {
                                    response["timing"] = json!({"queue_ms":started.duration_since(queued).as_secs_f64()*1000.0,
                                        "service_ms":started.elapsed().as_secs_f64()*1000.0});
                                    response
                                }
                                failure => {
                                    let failure_kind = if failure.is_err() { "deadline" } else { "transport_or_protocol" };
                                    child.stop().await;
                                    *guard = None;
                                    if let Some(lease) = lease.as_mut() { lease.retire(); }
                                    let mut response = error(&request, "worker_failed_no_automatic_retry", !read);
                                    response["worker_failure"] = json!({"kind":failure_kind,"deadline_ms":WAIT.as_millis()});
                                    response["timing"] = json!({"queue_ms":started.duration_since(queued).as_secs_f64()*1000.0,
                                        "service_ms":started.elapsed().as_secs_f64()*1000.0});
                                    response
                                }
                            }
                        }
                    }
                }
            };
            response["lane"] = json!(if read { "read" } else { "write" });
            drop(lease);
            drop(maintenance_permit);
            // Clear busy before delivery, allowing a caller to submit its next
            // dependent operation as soon as it receives this response.
            busy.lock().await.remove(&request.session);
            let _ = out.send(response).await;
        });
    }
    while jobs.join_next().await.is_some() {}
    for worker in &workers {
        if let Some(mut child) = worker.lock().await.take() {
            child.stop().await;
        }
    }
    drop(out);
    output_task.await.map_err(|_| ())??;
    if transport_failed { Err(()) } else { Ok(()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn bounded_frames_reject_oversize_and_truncation() {
        let mut input = BufReader::new(&b"ok\n"[..]);
        assert_eq!(frame(&mut input, 3).await.unwrap().unwrap(), b"ok\n");
        assert!(frame(&mut input, 3).await.unwrap().is_none());
        let mut input = BufReader::new(&b"oversize\n"[..]);
        assert!(frame(&mut input, 3).await.is_err());
        let mut input = BufReader::new(&b"partial"[..]);
        assert!(frame(&mut input, 30).await.is_err());
    }
}
