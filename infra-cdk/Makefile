# Makefile for code quality and formatting

# Define color codes
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m  # No Color

# Default target - run both lint and test
all: lint test

# Run tests
# TODO

# Run both linting and formatting in one command
lint: ruff-lint format eslint

# Run ESLint on frontend code
eslint:
	cd frontend && npm run lint

# Run linting checks and fix issues automatically
ruff-lint:
	ruff check --fix

# Format code according to project standards
format:
	ruff format

# CI/CD version of lint that only checks but doesn't modify files
lint-cicd:
	@echo "Running code quality checks..."
	@if ! ruff check; then \
		echo -e "$(RED)ERROR: Ruff linting failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make ruff-lint' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@if ! ruff format --check; then \
		echo -e "$(RED)ERROR: Code formatting check failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make format' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@echo -e "$(GREEN)All code quality checks passed!$(NC)"

PROFILE      = bedrock-agentcore-rag
STACK_NAME   = FAST-stack
FRONTEND_DIR = ../frontend
ZIP_NAME     = frontend-build.zip

.PHONY: deploy build-frontend upload-zip clean

deploy: build-frontend upload-zip
	cdk deploy --profile $(PROFILE)

first-deploy: build-frontend
	@echo "$(YELLOW)First deploy: creating infra (Lambda step may fail — this is expected)$(NC)"
	cdk deploy --profile $(PROFILE) || true
	@echo "$(GREEN)Bucket created. Now uploading zip and re-deploying...$(NC)"
	$(MAKE) upload-zip
	cdk deploy --profile $(PROFILE)

build-frontend:
	@echo "Building frontend..."
	cd $(FRONTEND_DIR) && npm run build
	@echo "Zipping build output..."
	cd $(FRONTEND_DIR)/build && python3 -c "import shutil; shutil.make_archive('../../infra-cdk/$(ZIP_NAME:.zip=)', 'zip', '.')"
	@echo "$(ZIP_NAME) ready."

upload-zip:
	$(eval BUCKET := $(shell aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--profile $(PROFILE) \
		--query "Stacks[0].Outputs[?OutputKey=='StagingBucketName'].OutputValue" \
		--output text 2>/dev/null))
	@if [ -z "$(BUCKET)" ]; then \
		echo "$(RED)ERROR: Staging bucket not found. Run 'make first-deploy' on first use.$(NC)"; \
		exit 1; \
	fi
	@echo "Uploading $(ZIP_NAME) to s3://$(BUCKET)/frontend/$(ZIP_NAME)..."
	aws s3 cp $(ZIP_NAME) s3://$(BUCKET)/frontend/$(ZIP_NAME) --profile $(PROFILE)
	@echo "$(GREEN)Upload complete.$(NC)"

clean:
	rm -f $(ZIP_NAME)