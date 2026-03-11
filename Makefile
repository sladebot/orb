.PHONY: test install-hooks

test:
	pytest tests/ \
	  --ignore=tests/integration \
	  --ignore=tests/test_triangle.py \
	  --ignore=tests/test_orchestrator.py \
	  -v --tb=short

install-hooks:
	cp scripts/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Pre-commit hook installed."
