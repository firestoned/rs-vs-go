// cmwatch-go: watch ConfigMaps in the pod's own namespace using a client-go informer.
//
// Output contract (identical to cmwatch-rs, parsed by scripts/bench.sh and analyze.py):
//
//	START impl=go workers=<n> list_mode=<list|streaming> http2=<on|off> wire=json watch_timeout_s=<n> tcp_keepalive=off
//	INIT <name>                  object delivered by the initial list
//	SYNCED size=<n> elapsed_ms=<ms>
//	APPLY <name>                 add or update after the initial sync
//	DELETE <name>
//	RTM ts=<unix> k=v ...        runtime snapshot, only on SIGUSR1
//	WATCH_ERROR <err>            (stderr)
//	EXIT applies=<n> deletes=<n>
package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/signal"
	"runtime"
	"runtime/metrics"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
)

// Runtime counters reported at each phase boundary. Deltas between snapshots attribute GC,
// scavenger and heap-release activity to phases. Unsupported names are skipped.
var rtmNames = []string{
	"/gc/cycles/total:gc-cycles",
	"/gc/cycles/automatic:gc-cycles",
	"/gc/cycles/forced:gc-cycles",
	"/cpu/classes/total:cpu-seconds",
	"/cpu/classes/user:cpu-seconds",
	"/cpu/classes/gc/total:cpu-seconds",
	"/cpu/classes/scavenge/total:cpu-seconds",
	"/cpu/classes/idle:cpu-seconds",
	"/memory/classes/total:bytes",
	"/memory/classes/heap/objects:bytes",
	"/memory/classes/heap/free:bytes",
	"/memory/classes/heap/released:bytes",
	"/gc/heap/goal:bytes",
	"/gc/heap/allocs:bytes",
	"/sched/goroutines:goroutines",
}

var keyFmt = strings.NewReplacer("/", "_", ":", "_", "-", "_")

func snapshot(cache int, applies, deletes uint64) {
	samples := make([]metrics.Sample, len(rtmNames))
	for i, n := range rtmNames {
		samples[i].Name = n
	}
	metrics.Read(samples)
	var b strings.Builder
	fmt.Fprintf(&b, "RTM ts=%.6f impl=go cache=%d applies=%d deletes=%d",
		float64(time.Now().UnixNano())/1e9, cache, applies, deletes)
	for _, s := range samples {
		key := strings.TrimPrefix(keyFmt.Replace(s.Name), "_")
		switch s.Value.Kind() {
		case metrics.KindUint64:
			fmt.Fprintf(&b, " %s=%d", key, s.Value.Uint64())
		case metrics.KindFloat64:
			fmt.Fprintf(&b, " %s=%.6f", key, s.Value.Float64())
		}
	}
	fmt.Println(b.String())
}

func main() {
	// Register SIGUSR1 first: its default action is to terminate.
	usr1 := make(chan os.Signal, 1)
	signal.Notify(usr1, syscall.SIGUSR1)

	started := time.Now()
	ns := os.Getenv("POD_NAMESPACE")
	if ns == "" {
		fmt.Fprintln(os.Stderr, "POD_NAMESPACE not set")
		os.Exit(1)
	}
	listMode := os.Getenv("LIST_MODE")
	if listMode == "" {
		listMode = "list"
	}
	http2 := "on"
	if os.Getenv("DISABLE_HTTP2") != "" { // honored by client-go's transport setup
		http2 = "off"
	}
	// Server-side watch timeout, identical on both sides. client-go's default is a random
	// 300-600 s; kube-rs defaults to 290 s. Also sent on the list, as kube-rs does.
	watchTimeout := int64(290)
	if v := os.Getenv("WATCH_TIMEOUT_S"); v != "" {
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil || n <= 0 {
			fmt.Fprintln(os.Stderr, "invalid WATCH_TIMEOUT_S")
			os.Exit(1)
		}
		watchTimeout = n
	}
	// Worker count comes from the GOMAXPROCS env var, set identically to TOKIO_WORKER_THREADS
	// in the pod spec. Setting it explicitly also disables the Go 1.25+ periodic cgroup re-check.
	fmt.Printf("START impl=go workers=%d list_mode=%s http2=%s wire=json watch_timeout_s=%d tcp_keepalive=off\n",
		runtime.GOMAXPROCS(0), listMode, http2, watchTimeout)

	cfg, err := rest.InClusterConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	// JSON on the wire, like kube-rs (which has no protobuf support). Without this, client-go
	// typed clients negotiate protobuf for built-in types such as ConfigMap.
	cfg.ContentType = "application/json"
	cfg.AcceptContentTypes = "application/json"
	// No TCP keepalive, like kube-rs (hyper's HttpConnector leaves SO_KEEPALIVE off).
	// client-go's default dialer sends a keepalive probe every 30 s.
	cfg.Dial = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: -1}).DialContext
	cs, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	// Resync 0: no periodic resync, matching the kube-rs watcher.
	factory := informers.NewSharedInformerFactoryWithOptions(cs, 0, informers.WithNamespace(ns),
		informers.WithTweakListOptions(func(o *metav1.ListOptions) {
			// Applied after the reflector sets its randomized timeout, so this wins.
			o.TimeoutSeconds = &watchTimeout
		}))
	inf := factory.Core().V1().ConfigMaps().Informer()
	_ = inf.SetWatchErrorHandler(func(_ *cache.Reflector, err error) {
		fmt.Fprintf(os.Stderr, "WATCH_ERROR %v\n", err)
	})

	var applies, deletes atomic.Uint64
	_, err = inf.AddEventHandler(cache.ResourceEventHandlerDetailedFuncs{
		AddFunc: func(obj interface{}, isInInitialList bool) {
			cm := obj.(*corev1.ConfigMap)
			if isInInitialList {
				fmt.Printf("INIT %s\n", cm.Name)
				return
			}
			applies.Add(1)
			fmt.Printf("APPLY %s\n", cm.Name)
		},
		UpdateFunc: func(_, obj interface{}) {
			applies.Add(1)
			fmt.Printf("APPLY %s\n", obj.(*corev1.ConfigMap).Name)
		},
		DeleteFunc: func(obj interface{}) {
			name := "unknown"
			switch o := obj.(type) {
			case *corev1.ConfigMap:
				name = o.Name
			case cache.DeletedFinalStateUnknown:
				name = o.Key
			}
			deletes.Add(1)
			fmt.Printf("DELETE %s\n", name)
		},
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	go func() {
		for range usr1 {
			snapshot(len(inf.GetStore().ListKeys()), applies.Load(), deletes.Load())
		}
	}()

	factory.Start(ctx.Done())
	if !cache.WaitForCacheSync(ctx.Done(), inf.HasSynced) {
		fmt.Fprintln(os.Stderr, "cache sync failed")
		os.Exit(1)
	}
	fmt.Printf("SYNCED size=%d elapsed_ms=%d\n", len(inf.GetStore().ListKeys()), time.Since(started).Milliseconds())

	<-ctx.Done()
	factory.Shutdown()
	fmt.Printf("EXIT applies=%d deletes=%d\n", applies.Load(), deletes.Load())
}
