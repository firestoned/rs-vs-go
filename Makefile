# Build on your workstation (docker or podman). Nothing here touches Docker on the k0s nodes.
BUILDER   ?= docker
PLATFORM  ?= linux/amd64
RS_IMAGE  ?= registry.local/cmbench/cmwatch-rs:0.3.0
GO_IMAGE  ?= registry.local/cmbench/cmwatch-go:0.3.0
PERF_IMAGE ?= registry.local/cmbench/perf:0.3.0
# ssh target for side-loading into k0s containerd, e.g. admin@worker-1
NODE_SSH  ?=
# add $(PERF_IMAGE) here when using ENABLE_PERF=true
LOAD_EXTRA ?=

.PHONY: build build-rs build-go build-perf push load bench analyze clean

build: build-rs build-go

build-rs:
	$(BUILDER) build --platform $(PLATFORM) -t $(RS_IMAGE) rust

build-go:
	$(BUILDER) build --platform $(PLATFORM) -t $(GO_IMAGE) go

# Only needed for ENABLE_PERF=true (hardware cycle counters).
build-perf:
	$(BUILDER) build --platform $(PLATFORM) -t $(PERF_IMAGE) perf

# Option A: you have a registry the cluster can pull from.
push:
	$(BUILDER) push $(RS_IMAGE)
	$(BUILDER) push $(GO_IMAGE)

# Option B: no registry. Import straight into k0s's containerd (k8s.io namespace) on the node.
load:
	@test -n "$(NODE_SSH)" || (echo "set NODE_SSH=user@node" && exit 1)
	for img in $(RS_IMAGE) $(GO_IMAGE) $(LOAD_EXTRA); do \
	  $(BUILDER) save $$img | ssh $(NODE_SSH) 'cat > /tmp/cmbench-img.tar && sudo k0s ctr -n k8s.io images import /tmp/cmbench-img.tar && rm -f /tmp/cmbench-img.tar'; \
	done

bench:
	./scripts/bench.sh

analyze:
	python3 scripts/analyze.py results/latest

clean:
	rm -rf rust/target
