.PHONY: test lint demo dashboard doctor live

test:
	PYTHONPATH=src python -m unittest discover -s tests -v

lint:
	python -m compileall -q src tests
	PYTHONPATH=src python -m loopgraph.cli --help >/dev/null

demo:
	PYTHONPATH=src python -m loopgraph.cli --db .loopgraph/rsi-demo.db rsi-demo --mode replay --auto-approve --reset

dashboard:
	PYTHONPATH=src python -m loopgraph.cli --db .loopgraph/dashboard.db dashboard --workspace .

doctor:
	PYTHONPATH=src python -m loopgraph.cli dsh-doctor

live:
	PYTHONPATH=src python -m loopgraph.cli --db .loopgraph/live.db rsi-demo --mode dsh --workspace . --auto-approve --reset
