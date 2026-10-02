//! cmwatch-rs: watch ConfigMaps in the pod's own namespace using a kube-rs reflector.
//!
//! Output contract (identical to cmwatch-go, parsed by scripts/bench.sh and analyze.py):
//!   START impl=rust workers=<n> list_mode=<list|streaming> http2=off wire=json watch_timeout_s=<n> tcp_keepalive=off
//!   INIT <name>                  object delivered by the initial list
//!   SYNCED size=<n> elapsed_ms=<ms>
//!   APPLY <name>                 add or update after the initial sync
//!   DELETE <name>
//!   RTM ts=<unix> k=v ...        runtime/allocator snapshot, only on SIGUSR1
//!   WATCH_ERROR <err>            (stderr)
//!   EXIT applies=<n> deletes=<n>
use std::{
    pin::pin,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use futures::StreamExt;
use k8s_openapi::api::core::v1::ConfigMap;
use kube::{
    Api, Client, ResourceExt,
    runtime::{WatchStreamExt, reflector, watcher},
};
use tokio::signal::unix::{SignalKind, signal};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Register signal handlers first: SIGUSR1's default action is to terminate.
    let mut usr1 = signal(SignalKind::user_defined1())?;
    let mut term = signal(SignalKind::terminate())?;

    let started = Instant::now();
    let ns = std::env::var("POD_NAMESPACE")?;
    let streaming = std::env::var("LIST_MODE").is_ok_and(|v| v == "streaming");
    // Server-side watch timeout, identical on both sides (kube-rs default, set explicitly).
    let watch_timeout: u32 = match std::env::var("WATCH_TIMEOUT_S") {
        Ok(v) => v.parse().map_err(|_| anyhow::anyhow!("invalid WATCH_TIMEOUT_S"))?,
        Err(_) => 290,
    };
    // Worker count comes from the TOKIO_WORKER_THREADS env var, set identically to GOMAXPROCS
    // in the pod spec. kube-rs 4.2 speaks HTTP/1.1 with JSON and sets no TCP keepalive; the Go
    // binary is configured to match, and both report it here.
    let workers = tokio::runtime::Handle::current().metrics().num_workers();
    println!(
        "START impl=rust workers={workers} list_mode={} http2=off wire=json watch_timeout_s={watch_timeout} tcp_keepalive=off",
        if streaming { "streaming" } else { "list" }
    );

    let client = Client::try_default().await?;
    let api: Api<ConfigMap> = Api::namespaced(client, &ns);
    let mut cfg = watcher::Config::default().timeout(watch_timeout);
    if streaming {
        cfg = cfg.streaming_lists();
    } else {
        // resourceVersion=0 on the initial list, the same as client-go's reflector.
        cfg = cfg.any_semantic();
    }

    // Reflector keeps a full in-memory cache, the same as a client-go informer store.
    let (reader, writer) = reflector::store();
    let mut stream = pin!(reflector(writer, watcher(api, cfg)).default_backoff());
    let (mut applies, mut deletes) = (0u64, 0u64);

    loop {
        tokio::select! {
            _ = term.recv() => break,
            _ = usr1.recv() => snapshot(reader.state().len(), applies, deletes),
            ev = stream.next() => match ev {
                None => break,
                Some(Ok(watcher::Event::Init)) => {}
                Some(Ok(watcher::Event::InitApply(cm))) => println!("INIT {}", cm.name_any()),
                Some(Ok(watcher::Event::InitDone)) => println!(
                    "SYNCED size={} elapsed_ms={}",
                    reader.state().len(),
                    started.elapsed().as_millis()
                ),
                Some(Ok(watcher::Event::Apply(cm))) => {
                    applies += 1;
                    println!("APPLY {}", cm.name_any());
                }
                Some(Ok(watcher::Event::Delete(cm))) => {
                    deletes += 1;
                    println!("DELETE {}", cm.name_any());
                }
                Some(Err(e)) => eprintln!("WATCH_ERROR {e}"),
            }
        }
    }

    println!("EXIT applies={applies} deletes={deletes}");
    Ok(())
}

/// One line of allocator (glibc malloc) and async-runtime internals. The Go counterpart
/// reports runtime/metrics. Neither side does any periodic work to produce this.
fn snapshot(cache: usize, applies: u64, deletes: u64) {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default();
    // SAFETY: mallinfo2 takes no arguments and only reads allocator state (glibc >= 2.33).
    let mi = unsafe { libc::mallinfo2() };
    let rt = tokio::runtime::Handle::current().metrics();
    println!(
        "RTM ts={ts:.6} impl=rust cache={cache} applies={applies} deletes={deletes} \
         malloc_arena_bytes={} malloc_mmap_bytes={} malloc_in_use_bytes={} \
         malloc_free_bytes={} malloc_releasable_bytes={} \
         tokio_alive_tasks={} tokio_global_queue={}",
        mi.arena,
        mi.hblkhd,
        mi.uordblks,
        mi.fordblks,
        mi.keepcost,
        rt.num_alive_tasks(),
        rt.global_queue_depth(),
    );
}
