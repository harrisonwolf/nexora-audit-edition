.PHONY: test compile tree verify

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v

compile:
	python3 -m compileall -q src tests scripts

tree:
	python3 scripts/verify_tree.py

verify: test compile tree
