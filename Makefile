.PHONY: fmt lint test package tf-init tf-plan tf-apply

fmt:
	ruff format .
	ruff check --fix .

lint:
	ruff format --check .
	ruff check .

test:
	pytest

package:
	mkdir -p dist
	cd lambda/proxy && zip -r ../../dist/proxy.zip . -x '*.pyc' -x '__pycache__/*'

tf-init:
	terraform -chdir=terraform/main init

tf-plan:
	terraform -chdir=terraform/main plan

tf-apply:
	terraform -chdir=terraform/main apply
