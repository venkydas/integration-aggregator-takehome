SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

BAO_NS        ?= openbao
APP_NS        ?= aggregator

# OpenBao image (with the oauthapp plugin). Defaults to the locally built
# image; CI overrides these to point at ghcr.io.
BAO_IMAGE_REGISTRY ?= docker.io
BAO_IMAGE_REPO     ?= library/openbao-oauthapp
BAO_IMAGE_TAG      ?= dev

MINIKUBE_FLAGS ?= --driver=docker --cpus=2 --memory=4096

.PHONY: help up down cluster openbao bootstrap images status

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

up: cluster openbao bootstrap ## Create/update everything (idempotent)

down: ## Tear everything down
	minikube delete || true

cluster: ## Start minikube if it is not running
	@minikube status >/dev/null 2>&1 || minikube start $(MINIKUBE_FLAGS)

images: ## Build images locally and load them into minikube
	docker build -t openbao-oauthapp:dev openbao/
	minikube image load openbao-oauthapp:dev

openbao: ## Install/upgrade OpenBao from its Helm chart
	helm repo add openbao https://openbao.github.io/openbao-helm >/dev/null 2>&1 || true
	helm upgrade --install openbao openbao/openbao \
	  --version 0.30.3 \
	  --namespace $(BAO_NS) --create-namespace \
	  -f deploy/openbao-values.yaml \
	  --set server.image.registry=$(BAO_IMAGE_REGISTRY) \
	  --set server.image.repository=$(BAO_IMAGE_REPO) \
	  --set server.image.tag=$(BAO_IMAGE_TAG) \
	  --wait=false

bootstrap: ## Init, unseal and configure OpenBao
	BAO_NAMESPACE=$(BAO_NS) APP_NAMESPACE=$(APP_NS) bash scripts/bootstrap-bao.sh

status: ## Show what is running
	kubectl get pods -n $(BAO_NS)
