# Offline dev/test entry points. Nothing here touches launchd, ~/Library, or
# a MyFitnessPal account.
SHELL := /bin/bash

TOOLS := .tools
BATS_VERSION := v1.11.1
SHELLCHECK_VERSION := v0.10.0
SHELLCHECK_ARCH := $(if $(filter arm64,$(shell uname -m)),aarch64,x86_64)

# Pinned, repo-local copies so a different `bats` on PATH can't shadow bats-core.
BATS := $(TOOLS)/bats-core/bin/bats
SHELLCHECK := $(TOOLS)/shellcheck

SH_FILES := $(wildcard bin/mfpctl scripts/*.sh scripts/lib/*.sh scripts/dev/*.sh) tests/helpers.bash

.PHONY: tools test lint verify clean-tools

tools: $(BATS) $(SHELLCHECK)

$(BATS):
	@mkdir -p $(TOOLS)
	git -c advice.detachedHead=false clone -q --depth 1 --branch $(BATS_VERSION) \
	  https://github.com/bats-core/bats-core.git $(TOOLS)/bats-core

$(SHELLCHECK):
	@mkdir -p $(TOOLS)
	curl -fsSL https://github.com/koalaman/shellcheck/releases/download/$(SHELLCHECK_VERSION)/shellcheck-$(SHELLCHECK_VERSION).darwin.$(SHELLCHECK_ARCH).tar.xz \
	  | tar -xJ -C $(TOOLS) --strip-components 1 shellcheck-$(SHELLCHECK_VERSION)/shellcheck

test: $(BATS)
	$(BATS) tests

lint: $(SHELLCHECK)
	$(SHELLCHECK) -x $(SH_FILES)
	@if [ -x scripts/dev/lint-plists.sh ]; then scripts/dev/lint-plists.sh; fi

verify: lint test

clean-tools:
	rm -rf $(TOOLS)
