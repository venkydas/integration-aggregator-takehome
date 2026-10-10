SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

BAO_NS ?= openbao
APP_NS ?= aggregator

# Full image references. Leave empty to build locally and load into minikube
# (content-tagged by scripts/build-images.sh). CI sets them to ghcr.io images.
APP_IMAGE ?=
BAO_IMAGE ?=

# Chart to deploy the app from: the local directory, or the published OCI chart
# in CI (e.g. CHART=oci://ghcr.io/venkydas/charts/aggregator CHART_VERSION=0.1.0).
CHART         ?= charts/aggregator
CHART_VERSION ?=

MINIKUBE_FLAGS ?= --driver=docker --cpus=2 --memory=4096

APP_REF = $(or $(APP_IMAGE),$$(cat .build/aggregator.ref))
BAO_REF = $(or $(BAO_IMAGE),$$(cat .build/openbao-oauthapp.ref))

.PHONY: help up down cluster images openbao bootstrap app test lint port-forward status

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

up: cluster $(if $(APP_IMAGE),,images) openbao bootstrap app ## Create/update everything (idempotent)

down: ## Tear everything down (idempotent)
	minikube delete
	rm -rf .build

cluster: ## Start minikube if it is not running
	@minikube status >/dev/null 2>&1 || minikube start $(MINIKUBE_FLAGS)

images: ## Build images locally and load them into minikube
	bash scripts/build-images.sh

openbao: ## Install/upgrade OpenBao from its Helm chart
	helm repo add openbao https://openbao.github.io/openbao-helm >/dev/null 2>&1 || true
	ref=$(BAO_REF); \
	helm upgrade --install openbao openbao/openbao --version 0.30.3 \
	  --namespace $(BAO_NS) --create-namespace \
	  -f deploy/openbao-values.yaml \
	  --set server.image.registry=$${ref%%/*} \
	  --set server.image.repository=$$(r=$${ref#*/}; echo $${r%:*}) \
	  --set server.image.tag=$${ref##*:}

bootstrap: ## Init, unseal and configure OpenBao
	BAO_NAMESPACE=$(BAO_NS) APP_NAMESPACE=$(APP_NS) bash scripts/bootstrap-bao.sh

app: ## Install/upgrade the aggregator from its Helm chart
	ref=$(APP_REF); \
	helm upgrade --install aggregator $(CHART) $(if $(CHART_VERSION),--version $(CHART_VERSION)) \
	  --namespace $(APP_NS) --create-namespace \
	  --set image.repository=$${ref%:*} \
	  --set image.tag=$${ref##*:} \
	  --wait --timeout 5m

test: ## Unit tests
	uv run pytest -q

lint: ## Lint and format check
	uv run ruff check .
	uv run ruff format --check .

port-forward: ## Expose the service on localhost:8080
	kubectl -n $(APP_NS) port-forward svc/aggregator 8080:8080

status: ## Show what is running
	kubectl get pods -n $(BAO_NS)
	kubectl get pods -n $(APP_NS)
