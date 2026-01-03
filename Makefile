.PHONY: help test coverage lint format install clean

help:
	@echo "Available commands:"
	@echo "  make install          - Install dependencies"
	@echo "  make test             - Run all tests"
	@echo "  make test-fast        - Run tests without coverage"
	@echo "  make coverage         - Run tests with coverage report"
	@echo "  make lint             - Run linting checks"
	@echo "  make format           - Format code with black and isort"
	@echo "  make clean            - Remove build artifacts"

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

test:
	pytest -v

test-fast:
	pytest -v --tb=line

coverage:
	pytest \
		--cov=cline_telegram_bot \
		--cov-report=html \
		--cov-report=term-missing \
		-v
	@echo "\nCoverage report generated. Open htmlcov/index.html to view."

lint:
	flake8 *.py --max-line-length=127
	black --check --line-length=127 *.py
	isort --profile black --check-only *.py

format:
	black --line-length=127 *.py
	isort --profile black *.py

clean:
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '*.egg-info' -delete
	rm -rf .pytest_cache .coverage htmlcov/ dist/ build/